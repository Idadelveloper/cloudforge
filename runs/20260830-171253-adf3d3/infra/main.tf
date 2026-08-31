provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project      = var.project_name
      "managed-by" = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

#############################################
# Customer managed KMS key (encryption at rest
# for the DynamoDB table, Lambda environment
# variables and CloudWatch log groups)
#############################################
resource "aws_kms_key" "notes" {
  description             = "CMK for ${var.project_name} DynamoDB table, Lambda env vars and CloudWatch logs"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableAccountAdministration"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowCloudWatchLogsUseOfKey"
        Effect = "Allow"
        Principal = {
          Service = "logs.${data.aws_region.current.name}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncryptFrom",
          "kms:ReEncryptTo",
          "kms:GenerateDataKey",
          "kms:GenerateDataKeyWithoutPlaintext",
          "kms:DescribeKey"
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:*"
          }
        }
      }
    ]
  })
}

resource "aws_kms_alias" "notes" {
  name          = "alias/${var.project_name}-notes"
  target_key_id = aws_kms_key.notes.key_id
}

#############################################
# DynamoDB: notes-table
#############################################
resource "aws_dynamodb_table" "notes" {
  #checkov:skip=CKV2_AWS_16:On-demand (PAY_PER_REQUEST) billing needs no autoscaling target.
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "user_id"
  range_key    = "note_id"

  attribute {
    name = "user_id"
    type = "S"
  }

  attribute {
    name = "note_id"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.notes.arn
  }

  point_in_time_recovery {
    enabled = true
  }

  deletion_protection_enabled = false

  tags = {
    Name = var.dynamodb_table_name
  }
}

#############################################
# CloudWatch: notes-api-log-group (+ API access logs)
#############################################
resource "aws_cloudwatch_log_group" "lambda" {
  name              = var.lambda_log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.notes.arn

  tags = {
    Name = var.lambda_log_group_name
  }
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = var.api_gateway_log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.notes.arn

  tags = {
    Name = var.api_gateway_log_group_name
  }
}

#############################################
# IAM: notes-api-lambda-role (least privilege)
#############################################
resource "aws_iam_role" "lambda" {
  name = var.lambda_role_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "LambdaAssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = var.lambda_role_name
  }
}

resource "aws_iam_policy" "lambda" {
  name        = "${var.lambda_role_name}-policy"
  description = "Least-privilege access to the notes table, its CMK and the function log group"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "NotesTableCrud"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query"
        ]
        Resource = [
          aws_dynamodb_table.notes.arn,
          "${aws_dynamodb_table.notes.arn}/index/*"
        ]
      },
      {
        Sid    = "NotesTableKeyUsage"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey"
        ]
        Resource = [aws_kms_key.notes.arn]
      },
      {
        Sid    = "FunctionLogging"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = [
          aws_cloudwatch_log_group.lambda.arn,
          "${aws_cloudwatch_log_group.lambda.arn}:*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda" {
  role       = aws_iam_role.lambda.name
  policy_arn = aws_iam_policy.lambda.arn
}

#############################################
# Lambda: notes-api-function (inline packaged)
#############################################
resource "local_file" "handler" {
  filename        = "${path.module}/build/handler.py"
  file_permission = "0644"

  content = <<PYTHON
import base64
import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

TABLE_NAME = os.environ.get("NOTES_TABLE_NAME", "notes-table")
DEFAULT_USER_ID = os.environ.get("DEFAULT_USER_ID", "default-user")
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

_TABLE = boto3.resource("dynamodb").Table(TABLE_NAME)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reply(status, body=None):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": "" if body is None else json.dumps(body),
    }


def _error(status, detail, code):
    return _reply(status, {"detail": detail, "code": code})


def _user_id(event):
    headers = event.get("headers") or {}
    for name, value in headers.items():
        if name.lower() == "x-user-id" and value:
            return value
    return DEFAULT_USER_ID


def _parse_body(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    if not raw.strip():
        return {}
    return json.loads(raw)


def _validate(title, body):
    if not isinstance(title, str) or not 1 <= len(title) <= 200:
        return "title must be a string of 1-200 characters"
    if not isinstance(body, str) or len(body) > 10000:
        return "body must be a string of at most 10000 characters"
    return None


def _encode_token(key):
    return base64.urlsafe_b64encode(json.dumps(key).encode("utf-8")).decode("utf-8")


def _decode_token(token):
    return json.loads(base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8"))


def create_note(user_id, payload):
    title = payload.get("title")
    body = payload.get("body")
    problem = _validate(title, body)
    if problem:
        return _error(422, problem, "validation_error")
    stamp = _now()
    item = {
        "user_id": user_id,
        "note_id": str(uuid.uuid4()),
        "title": title,
        "body": body,
        "created_at": stamp,
        "updated_at": stamp,
    }
    _TABLE.put_item(Item=item)
    return _reply(201, item)


def list_notes(user_id, params):
    params = params or {}
    try:
        limit = int(params.get("limit") or DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        return _error(422, "limit must be an integer", "validation_error")
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    kwargs = {
        "KeyConditionExpression": Key("user_id").eq(user_id),
        "Limit": limit,
    }
    token = params.get("next_token")
    if token:
        try:
            kwargs["ExclusiveStartKey"] = _decode_token(token)
        except Exception:
            return _error(422, "next_token is not valid", "validation_error")
    result = _TABLE.query(**kwargs)
    items = result.get("Items", [])
    last = result.get("LastEvaluatedKey")
    return _reply(
        200,
        {
            "items": items,
            "next_token": _encode_token(last) if last else None,
            "count": len(items),
        },
    )


def get_note(user_id, note_id):
    item = _TABLE.get_item(Key={"user_id": user_id, "note_id": note_id}).get("Item")
    if not item:
        return _error(404, "Note not found", "not_found")
    return _reply(200, item)


def update_note(user_id, note_id, payload):
    item = _TABLE.get_item(Key={"user_id": user_id, "note_id": note_id}).get("Item")
    if not item:
        return _error(404, "Note not found", "not_found")
    title = payload.get("title", item.get("title"))
    body = payload.get("body", item.get("body"))
    problem = _validate(title, body)
    if problem:
        return _error(422, problem, "validation_error")
    item["title"] = title
    item["body"] = body
    item["updated_at"] = _now()
    _TABLE.put_item(Item=item)
    return _reply(200, item)


def delete_note(user_id, note_id):
    try:
        _TABLE.delete_item(
            Key={"user_id": user_id, "note_id": note_id},
            ConditionExpression="attribute_exists(note_id)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _error(404, "Note not found", "not_found")
        raise
    return _reply(204)


def handler(event, context):
    method = (event.get("httpMethod") or "GET").upper()
    path = event.get("path") or "/"
    segments = [part for part in path.split("/") if part]

    if segments and segments[0] == "health":
        if method != "GET":
            return _error(405, "Method not allowed", "method_not_allowed")
        return _reply(200, {"status": "ok", "service": "personal_notes_api"})

    if not segments or segments[0] != "notes" or len(segments) > 2:
        return _error(404, "Not found", "not_found")

    user_id = _user_id(event)
    note_id = segments[1] if len(segments) == 2 else None

    try:
        payload = _parse_body(event) if method in ("POST", "PUT") else {}
    except (ValueError, TypeError):
        return _error(400, "Request body must be valid JSON", "invalid_json")
    if not isinstance(payload, dict):
        return _error(400, "Request body must be a JSON object", "invalid_json")

    if note_id is None:
        if method == "POST":
            return create_note(user_id, payload)
        if method == "GET":
            return list_notes(user_id, event.get("queryStringParameters"))
        return _error(405, "Method not allowed", "method_not_allowed")

    if method == "GET":
        return get_note(user_id, note_id)
    if method == "PUT":
        return update_note(user_id, note_id, payload)
    if method == "DELETE":
        return delete_note(user_id, note_id)
    return _error(405, "Method not allowed", "method_not_allowed")
PYTHON
}

data "archive_file" "lambda" {
  type        = "zip"
  source_file = local_file.handler.filename
  output_path = "${path.module}/build/notes_api_function.zip"

  depends_on = [local_file.handler]
}

resource "aws_lambda_function" "notes_api" {
  #checkov:skip=CKV_AWS_117:LocalStack Community has no VPC networking for Lambda; the function only reaches DynamoDB via the AWS API.
  #checkov:skip=CKV_AWS_116:No async invocation path exists (API Gateway invokes synchronously), and the plan authorises no SQS/SNS DLQ resource.
  #checkov:skip=CKV_AWS_272:Code-signing configurations are not supported in LocalStack Community.
  function_name = var.lambda_function_name
  role          = aws_iam_role.lambda.arn
  handler       = "handler.handler"
  runtime       = var.lambda_runtime
  memory_size   = var.lambda_memory_size
  timeout       = var.lambda_timeout

  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  reserved_concurrent_executions = var.lambda_reserved_concurrency
  kms_key_arn                    = aws_kms_key.notes.arn

  environment {
    variables = {
      NOTES_TABLE_NAME = aws_dynamodb_table.notes.name
      DEFAULT_USER_ID  = var.default_user_id
      LOG_LEVEL        = "INFO"
    }
  }

  tracing_config {
    mode = "Active"
  }

  tags = {
    Name = var.lambda_function_name
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda,
    aws_cloudwatch_log_group.lambda
  ]
}

#############################################
# API Gateway: notes-api-gateway
#############################################
resource "aws_api_gateway_rest_api" "notes" {
  #checkov:skip=CKV2_AWS_51:No client-certificate mTLS backend authentication is required; the only integration target is a private Lambda invoked over IAM.
  #checkov:skip=CKV_AWS_237:Proxy REST API is redeployed via the deployment trigger hash with create_before_destroy on the deployment.
  name        = var.api_gateway_name
  description = "REST entry point for the personal notes API"

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  tags = {
    Name = var.api_gateway_name
  }
}

resource "aws_api_gateway_resource" "proxy" {
  rest_api_id = aws_api_gateway_rest_api.notes.id
  parent_id   = aws_api_gateway_rest_api.notes.root_resource_id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_method" "proxy" {
  #checkov:skip=CKV_AWS_59:The specification defines no authentication mechanism; callers are scoped by the X-User-Id header handled inside the application.
  #checkov:skip=CKV2_AWS_53:Request validation is performed by the application (Pydantic models) because this is a Lambda proxy integration.
  rest_api_id   = aws_api_gateway_rest_api.notes.id
  resource_id   = aws_api_gateway_resource.proxy.id
  http_method   = "ANY"
  authorization = "NONE"

  request_parameters = {
    "method.request.path.proxy" = true
  }
}

resource "aws_api_gateway_integration" "proxy" {
  rest_api_id             = aws_api_gateway_rest_api.notes.id
  resource_id             = aws_api_gateway_resource.proxy.id
  http_method             = aws_api_gateway_method.proxy.http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = aws_lambda_function.notes_api.invoke_arn
}

resource "aws_api_gateway_method" "root" {
  #checkov:skip=CKV_AWS_59:The specification defines no authentication mechanism; callers are scoped by the X-User-Id header handled inside the application.
  #checkov:skip=CKV2_AWS_53:Request validation is performed by the application (Pydantic models) because this is a Lambda proxy integration.
  rest_api_id   = aws_api_gateway_rest_api.notes.id
  resource_id   = aws_api_gateway_rest_api.notes.root_resource_id
  http_method   = "ANY"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "root" {
  rest_api_id             = aws_api_gateway_rest_api.notes.id
  resource_id             = aws_api_gateway_rest_api.notes.root_resource_id
  http_method             = aws_api_gateway_method.root.http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = aws_lambda_function.notes_api.invoke_arn
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowInvokeFromNotesApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notes_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.notes.execution_arn}/*/*"
}

resource "aws_api_gateway_deployment" "notes" {
  rest_api_id = aws_api_gateway_rest_api.notes.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.proxy.id,
      aws_api_gateway_method.proxy.id,
      aws_api_gateway_integration.proxy.id,
      aws_api_gateway_method.root.id,
      aws_api_gateway_integration.root.id,
      aws_lambda_function.notes_api.source_code_hash,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.proxy,
    aws_api_gateway_integration.root
  ]
}

resource "aws_api_gateway_stage" "notes" {
  #checkov:skip=CKV_AWS_120:API Gateway caching is unsupported in LocalStack Community and every route is a per-user DynamoDB read that must not be cached.
  #checkov:skip=CKV2_AWS_29:AWS WAF is not available in LocalStack Community, so no web ACL can be associated.
  #checkov:skip=CKV2_AWS_51:No client-certificate mTLS backend authentication is required for the private Lambda integration.
  rest_api_id           = aws_api_gateway_rest_api.notes.id
  deployment_id         = aws_api_gateway_deployment.notes.id
  stage_name            = var.api_stage_name
  xray_tracing_enabled  = true
  cache_cluster_enabled = false

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      resourcePath   = "$context.resourcePath"
      status         = "$context.status"
      protocol       = "$context.protocol"
      responseLength = "$context.responseLength"
    })
  }

  tags = {
    Name = "${var.api_gateway_name}-${var.api_stage_name}"
  }

  depends_on = [aws_cloudwatch_log_group.api_access]
}

resource "aws_api_gateway_method_settings" "notes" {
  #checkov:skip=CKV_AWS_225:Response caching is intentionally disabled for per-user note data.
  rest_api_id = aws_api_gateway_rest_api.notes.id
  stage_name  = aws_api_gateway_stage.notes.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled = true
    logging_level   = "INFO"
    caching_enabled = false
  }
}
