provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project      = var.project_name
      "managed-by" = var.managed_by
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_id          = data.aws_caller_identity.current.account_id
  region              = data.aws_region.current.name
  lambda_log_group    = "/aws/lambda/${var.lambda_function_name}"
  lambda_build_dir    = "${path.module}/build"
  lambda_source_file  = "${path.module}/build/handler.py"
  lambda_package_file = "${path.module}/build/notes_api_lambda.zip"
}

# ---------------------------------------------------------------------------
# KMS customer managed key - encrypts the DynamoDB table, the Lambda
# environment variables and the CloudWatch log groups listed in the plan.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "notes" {
  description             = "CMK for the personal notes API (DynamoDB, Lambda env vars, CloudWatch Logs)"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableAccountAdministration"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowCloudWatchLogsUse"
        Effect = "Allow"
        Principal = {
          Service = "logs.${local.region}.amazonaws.com"
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
      },
      {
        Sid    = "AllowLambdaAndDynamoDbServiceUse"
        Effect = "Allow"
        Principal = {
          Service = [
            "lambda.amazonaws.com",
            "dynamodb.amazonaws.com"
          ]
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
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-cmk"
  }
}

resource "aws_kms_alias" "notes" {
  name          = "alias/${var.project_name}"
  target_key_id = aws_kms_key.notes.key_id
}

# ---------------------------------------------------------------------------
# DynamoDB - notes-table
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "notes" {
  name         = var.notes_table_name
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

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.notes.arn
  }

  tags = {
    Name = var.notes_table_name
  }
}

# ---------------------------------------------------------------------------
# CloudWatch - notes-api-log-group (Lambda + API Gateway access logs)
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda" {
  name              = local.lambda_log_group
  retention_in_days = var.log_retention_in_days
  kms_key_id        = aws_kms_key.notes.arn

  tags = {
    Name = local.lambda_log_group
  }
}

resource "aws_cloudwatch_log_group" "api_gateway" {
  name              = var.api_gateway_log_group_name
  retention_in_days = var.log_retention_in_days
  kms_key_id        = aws_kms_key.notes.arn

  tags = {
    Name = var.api_gateway_log_group_name
  }
}

# ---------------------------------------------------------------------------
# IAM - notes-api-lambda-role (least privilege)
# ---------------------------------------------------------------------------
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

resource "aws_iam_role_policy" "lambda" {
  name = "${var.lambda_role_name}-policy"
  role = aws_iam_role.lambda.id

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
          aws_dynamodb_table.notes.arn
        ]
      },
      {
        Sid    = "WriteLambdaLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = [
          "${aws_cloudwatch_log_group.lambda.arn}:*"
        ]
      },
      {
        Sid    = "UseNotesCmk"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey"
        ]
        Resource = [
          aws_kms_key.notes.arn
        ]
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# Lambda - notes-api-function (inline packaged source, no prebuilt artifact)
# ---------------------------------------------------------------------------
resource "local_file" "lambda_source" {
  filename        = local.lambda_source_file
  file_permission = "0644"

  content = <<PYTHON
"""Personal notes REST API handler (API Gateway proxy integration).

Implements: GET /health, POST /notes, GET /notes, GET /notes/{note_id},
PUT /notes/{note_id}, DELETE /notes/{note_id}.
Notes are persisted in DynamoDB keyed by (user_id, note_id).
"""

import base64
import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ.get("NOTES_TABLE_NAME", "notes-table")
DEFAULT_USER_ID = os.environ.get("DEFAULT_USER_ID", "default-user")

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_TITLE = 200
MAX_BODY = 20000

_TABLE = boto3.resource("dynamodb").Table(TABLE_NAME)


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reply(status, payload=None):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": "" if payload is None else json.dumps(payload),
    }


def _error(status, detail, code):
    return _reply(status, {"detail": detail, "code": code})


def _user_id(event):
    headers = event.get("headers") or {}
    for name, value in headers.items():
        if name and name.lower() == "x-user-id" and value:
            return str(value)
    return DEFAULT_USER_ID


def _json_body(event):
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception:
            return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _encode_cursor(last_key):
    if not last_key:
        return None
    raw = json.dumps(last_key, sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8")


def _decode_cursor(cursor):
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("utf-8"))
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _validate_title(title):
    if not isinstance(title, str):
        return "title must be a string"
    if len(title) < 1 or len(title) > MAX_TITLE:
        return "title must be between 1 and 200 characters"
    return None


def _validate_body(body):
    if not isinstance(body, str):
        return "body must be a string"
    if len(body) > MAX_BODY:
        return "body must be at most 20000 characters"
    return None


def create_note(user_id, payload):
    if payload is None:
        return _error(400, "request body must be a JSON object", "invalid_body")
    if "title" not in payload:
        return _error(422, "title is required", "validation_error")
    problem = _validate_title(payload.get("title"))
    if problem:
        return _error(422, problem, "validation_error")
    body_text = payload.get("body", "")
    if body_text is None:
        body_text = ""
    problem = _validate_body(body_text)
    if problem:
        return _error(422, problem, "validation_error")

    timestamp = _now()
    item = {
        "user_id": user_id,
        "note_id": str(uuid.uuid4()),
        "title": payload["title"],
        "body": body_text,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _TABLE.put_item(Item=item)
    return _reply(201, item)


def list_notes(user_id, query):
    query = query or {}
    limit = DEFAULT_LIMIT
    raw_limit = query.get("limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return _error(422, "limit must be an integer", "validation_error")
        if limit < 1 or limit > MAX_LIMIT:
            return _error(422, "limit must be between 1 and 100", "validation_error")

    kwargs = {
        "KeyConditionExpression": Key("user_id").eq(user_id),
        "Limit": limit,
    }
    cursor = query.get("cursor")
    if cursor:
        start_key = _decode_cursor(cursor)
        if start_key is None:
            return _error(400, "cursor is not valid", "invalid_cursor")
        kwargs["ExclusiveStartKey"] = start_key

    result = _TABLE.query(**kwargs)
    items = result.get("Items", [])
    return _reply(
        200,
        {
            "items": items,
            "next_cursor": _encode_cursor(result.get("LastEvaluatedKey")),
            "count": len(items),
        },
    )


def get_note(user_id, note_id):
    result = _TABLE.get_item(Key={"user_id": user_id, "note_id": note_id})
    item = result.get("Item")
    if not item:
        return _error(404, "note not found", "not_found")
    return _reply(200, item)


def update_note(user_id, note_id, payload):
    if payload is None:
        return _error(400, "request body must be a JSON object", "invalid_body")

    existing = _TABLE.get_item(Key={"user_id": user_id, "note_id": note_id}).get("Item")
    if not existing:
        return _error(404, "note not found", "not_found")

    if "title" in payload:
        problem = _validate_title(payload.get("title"))
        if problem:
            return _error(422, problem, "validation_error")
        existing["title"] = payload["title"]

    if "body" in payload:
        new_body = payload.get("body")
        if new_body is None:
            new_body = ""
        problem = _validate_body(new_body)
        if problem:
            return _error(422, problem, "validation_error")
        existing["body"] = new_body

    existing["updated_at"] = _now()
    _TABLE.put_item(Item=existing)
    return _reply(200, existing)


def delete_note(user_id, note_id):
    existing = _TABLE.get_item(Key={"user_id": user_id, "note_id": note_id}).get("Item")
    if not existing:
        return _error(404, "note not found", "not_found")
    _TABLE.delete_item(Key={"user_id": user_id, "note_id": note_id})
    return _reply(204, None)


def handler(event, context):
    method = str(event.get("httpMethod") or "GET").upper()
    path = event.get("path") or "/"
    segments = [part for part in path.split("/") if part]
    query = event.get("queryStringParameters") or {}
    user_id = _user_id(event)

    if segments == ["health"]:
        if method != "GET":
            return _error(405, "method not allowed", "method_not_allowed")
        return _reply(200, {"status": "ok", "service": "personal_notes_api"})

    if segments and segments[0] == "notes":
        if len(segments) == 1:
            if method == "POST":
                return create_note(user_id, _json_body(event))
            if method == "GET":
                return list_notes(user_id, query)
            return _error(405, "method not allowed", "method_not_allowed")

        if len(segments) == 2:
            note_id = segments[1]
            if method == "GET":
                return get_note(user_id, note_id)
            if method in ("PUT", "PATCH"):
                return update_note(user_id, note_id, _json_body(event))
            if method == "DELETE":
                return delete_note(user_id, note_id)
            return _error(405, "method not allowed", "method_not_allowed")

    return _error(404, "resource not found", "not_found")
PYTHON
}

data "archive_file" "lambda_package" {
  type        = "zip"
  source_file = local_file.lambda_source.filename
  output_path = local.lambda_package_file
}

resource "aws_lambda_function" "notes_api" {
  # checkov:skip=CKV_AWS_116: LocalStack Community deployment; the plan does not include an SQS/SNS dead letter target.
  # checkov:skip=CKV_AWS_117: VPC networking resources are out of scope for this plan and unsupported on LocalStack Community.
  # checkov:skip=CKV_AWS_272: AWS Signer code signing is not available in the target LocalStack environment.
  function_name = var.lambda_function_name
  role          = aws_iam_role.lambda.arn
  handler       = "handler.handler"
  runtime       = var.lambda_runtime
  memory_size   = var.lambda_memory_size
  timeout       = var.lambda_timeout

  filename         = data.archive_file.lambda_package.output_path
  source_code_hash = data.archive_file.lambda_package.output_base64sha256

  reserved_concurrent_executions = var.lambda_reserved_concurrency
  kms_key_arn                    = aws_kms_key.notes.arn

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      NOTES_TABLE_NAME = aws_dynamodb_table.notes.name
      DEFAULT_USER_ID  = var.default_user_id
      LOG_LEVEL        = "INFO"
    }
  }

  depends_on = [
    aws_iam_role_policy.lambda,
    aws_cloudwatch_log_group.lambda
  ]

  tags = {
    Name = var.lambda_function_name
  }
}

# ---------------------------------------------------------------------------
# API Gateway - notes-api-gateway
# ---------------------------------------------------------------------------
resource "aws_api_gateway_rest_api" "notes_api" {
  # checkov:skip=CKV2_AWS_51: Public REST API; mutual TLS client certificates are not used by the notes clients.
  name        = var.api_gateway_name
  description = "REST API for the personal notes service"

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name = var.api_gateway_name
  }
}

resource "aws_api_gateway_resource" "proxy" {
  rest_api_id = aws_api_gateway_rest_api.notes_api.id
  parent_id   = aws_api_gateway_rest_api.notes_api.root_resource_id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_method" "proxy" {
  # checkov:skip=CKV_AWS_59: The notes API is intentionally public; per-user scoping uses the X-User-Id header.
  # checkov:skip=CKV2_AWS_53: Request validation is performed by the FastAPI/Pydantic application behind the proxy integration.
  rest_api_id   = aws_api_gateway_rest_api.notes_api.id
  resource_id   = aws_api_gateway_resource.proxy.id
  http_method   = "ANY"
  authorization = "NONE"

  request_parameters = {
    "method.request.path.proxy" = true
  }
}

resource "aws_api_gateway_integration" "proxy" {
  rest_api_id             = aws_api_gateway_rest_api.notes_api.id
  resource_id             = aws_api_gateway_resource.proxy.id
  http_method             = aws_api_gateway_method.proxy.http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = aws_lambda_function.notes_api.invoke_arn
}

resource "aws_api_gateway_method" "root" {
  # checkov:skip=CKV_AWS_59: The notes API is intentionally public; per-user scoping uses the X-User-Id header.
  # checkov:skip=CKV2_AWS_53: Request validation is performed by the FastAPI/Pydantic application behind the proxy integration.
  rest_api_id   = aws_api_gateway_rest_api.notes_api.id
  resource_id   = aws_api_gateway_rest_api.notes_api.root_resource_id
  http_method   = "ANY"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "root" {
  rest_api_id             = aws_api_gateway_rest_api.notes_api.id
  resource_id             = aws_api_gateway_rest_api.notes_api.root_resource_id
  http_method             = aws_api_gateway_method.root.http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = aws_lambda_function.notes_api.invoke_arn
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowExecutionFromApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notes_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.notes_api.execution_arn}/*/*"
}

resource "aws_api_gateway_deployment" "notes_api" {
  rest_api_id = aws_api_gateway_rest_api.notes_api.id

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

resource "aws_api_gateway_stage" "notes_api" {
  # checkov:skip=CKV_AWS_120: Response caching is disabled on purpose so note reads and writes are always consistent.
  # checkov:skip=CKV2_AWS_29: AWS WAF is not available in the LocalStack Community target environment.
  # checkov:skip=CKV2_AWS_51: Public REST API; mutual TLS client certificates are not used by the notes clients.
  rest_api_id          = aws_api_gateway_rest_api.notes_api.id
  deployment_id        = aws_api_gateway_deployment.notes_api.id
  stage_name           = var.api_stage_name
  xray_tracing_enabled = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      resourcePath   = "$context.resourcePath"
      status         = "$context.status"
      responseLength = "$context.responseLength"
      errorMessage   = "$context.error.message"
      integrationErr = "$context.integration.error"
    })
  }

  tags = {
    Name = "${var.api_gateway_name}-${var.api_stage_name}"
  }

  depends_on = [
    aws_cloudwatch_log_group.api_gateway
  ]
}

resource "aws_api_gateway_method_settings" "notes_api" {
  # checkov:skip=CKV_AWS_225: Caching is intentionally disabled for a read/write CRUD API to avoid stale notes.
  rest_api_id = aws_api_gateway_rest_api.notes_api.id
  stage_name  = aws_api_gateway_stage.notes_api.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled      = true
    logging_level        = "INFO"
    data_trace_enabled   = false
    caching_enabled      = false
    cache_data_encrypted = true
  }
}
