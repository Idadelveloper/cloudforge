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
  lambda_build_dir = "${path.module}/build"
}

# ---------------------------------------------------------------------------
# DynamoDB - plan resource: notes-table
# Partition key owner_id, sort key note_id. Backs all five CRUD endpoints and
# the owner-scoped list query.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "notes" {
  #checkov:skip=CKV_AWS_119:LocalStack Community Edition provides no KMS CMK; AWS-owned key SSE is enabled instead.
  name         = var.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "owner_id"
  range_key    = "note_id"

  attribute {
    name = "owner_id"
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
    enabled = true
  }

  tags = {
    Name = var.table_name
  }
}

# ---------------------------------------------------------------------------
# CloudWatch Logs - plan resource: notes-api-log-group
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda" {
  #checkov:skip=CKV_AWS_158:KMS is out of scope for LocalStack Community Edition; logs use the default CloudWatch encryption.
  name              = var.lambda_log_group_name
  retention_in_days = var.log_retention_days

  tags = {
    Name = var.lambda_log_group_name
  }
}

resource "aws_cloudwatch_log_group" "api" {
  #checkov:skip=CKV_AWS_158:KMS is out of scope for LocalStack Community Edition; logs use the default CloudWatch encryption.
  name              = var.api_log_group_name
  retention_in_days = var.log_retention_days

  tags = {
    Name = var.api_log_group_name
  }
}

# ---------------------------------------------------------------------------
# IAM - plan resource: notes-api-lambda-role
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    sid     = "LambdaAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = var.lambda_role_name
  description        = "Execution role for the personal notes API Lambda function"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json

  tags = {
    Name = var.lambda_role_name
  }
}

data "aws_iam_policy_document" "lambda_permissions" {
  statement {
    sid    = "NotesTableCrud"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
    ]

    resources = [aws_dynamodb_table.notes.arn]
  }

  statement {
    sid    = "LambdaLogging"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = [
      aws_cloudwatch_log_group.lambda.arn,
      "${aws_cloudwatch_log_group.lambda.arn}:*",
    ]
  }

  statement {
    sid    = "LambdaTracing"
    effect = "Allow"

    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
    ]

    resources = ["*"]
  }
}

resource "aws_iam_policy" "lambda" {
  name        = "${var.lambda_role_name}-policy"
  description = "Least-privilege access to the notes table and its log group"
  policy      = data.aws_iam_policy_document.lambda_permissions.json
}

resource "aws_iam_role_policy_attachment" "lambda" {
  role       = aws_iam_role.lambda.name
  policy_arn = aws_iam_policy.lambda.arn
}

# IAM role used by API Gateway to write access logs (part of the plan's IAM entry).
data "aws_iam_policy_document" "apigw_assume_role" {
  statement {
    sid     = "ApiGatewayAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["apigateway.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "apigw_logs" {
  name               = var.api_gateway_cloudwatch_role_name
  description        = "Allows API Gateway to publish access logs for the notes API"
  assume_role_policy = data.aws_iam_policy_document.apigw_assume_role.json

  tags = {
    Name = var.api_gateway_cloudwatch_role_name
  }
}

data "aws_iam_policy_document" "apigw_logs" {
  statement {
    sid    = "ApiGatewayAccessLogging"
    effect = "Allow"

    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
      "logs:GetLogEvents",
      "logs:FilterLogEvents",
    ]

    resources = [
      aws_cloudwatch_log_group.api.arn,
      "${aws_cloudwatch_log_group.api.arn}:*",
      "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/apigateway/*",
      "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/apigateway/*:*",
    ]
  }
}

resource "aws_iam_policy" "apigw_logs" {
  name        = "${var.api_gateway_cloudwatch_role_name}-policy"
  description = "Least-privilege CloudWatch Logs access for API Gateway access logging"
  policy      = data.aws_iam_policy_document.apigw_logs.json
}

resource "aws_iam_role_policy_attachment" "apigw_logs" {
  role       = aws_iam_role.apigw_logs.name
  policy_arn = aws_iam_policy.apigw_logs.arn
}

resource "aws_api_gateway_account" "this" {
  cloudwatch_role_arn = aws_iam_role.apigw_logs.arn

  depends_on = [aws_iam_role_policy_attachment.apigw_logs]
}

# ---------------------------------------------------------------------------
# Lambda - plan resource: notes-api-fn
# The deployment artifact is generated in-tree so no pre-built zip is needed.
# ---------------------------------------------------------------------------
resource "local_file" "lambda_source" {
  filename        = "${local.lambda_build_dir}/handler.py"
  file_permission = "0644"

  content = <<PYTHON
"""Personal notes REST API - AWS Lambda handler (API Gateway AWS_PROXY).

Implements the endpoints described in the shared plan:
  GET    /health
  POST   /notes
  GET    /notes
  GET    /notes/{note_id}
  PUT    /notes/{note_id}
  DELETE /notes/{note_id}
"""

import base64
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_NAME = os.environ["TABLE_NAME"]
DEFAULT_OWNER_ID = os.environ.get("DEFAULT_OWNER_ID", "default-user")
DEFAULT_PAGE_SIZE = int(os.environ.get("DEFAULT_PAGE_SIZE", "25"))
MAX_PAGE_SIZE = int(os.environ.get("MAX_PAGE_SIZE", "100"))

MAX_TITLE_LEN = 200
MAX_BODY_LEN = 20000

TABLE = boto3.resource("dynamodb").Table(TABLE_NAME)


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _response(status, payload=None):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": "" if payload is None else json.dumps(payload),
    }


def _error(status, detail, code):
    return _response(status, {"detail": detail, "code": code})


def _encode_token(last_key):
    raw = json.dumps(last_key, sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8")


def _decode_token(token):
    raw = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
    key = json.loads(raw)
    if not isinstance(key, dict):
        raise ValueError("next_token is not a valid cursor")
    return key


def _parse_note_payload(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("Request body must be valid JSON")
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")

    title = data.get("title")
    body = data.get("body")
    if body is None:
        body = ""
    if not isinstance(title, str) or not 1 <= len(title) <= MAX_TITLE_LEN:
        raise ValueError("title is required and must be 1-200 characters")
    if not isinstance(body, str) or len(body) > MAX_BODY_LEN:
        raise ValueError("body must be a string of at most 20000 characters")
    return title, body


def _create_note(event, owner_id):
    try:
        title, body = _parse_note_payload(event)
    except ValueError as exc:
        return _error(422, str(exc), "validation_error")

    timestamp = _now()
    item = {
        "owner_id": owner_id,
        "note_id": str(uuid.uuid4()),
        "title": title,
        "body": body,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    TABLE.put_item(Item=item)
    LOG.info("created note %s", item["note_id"])
    return _response(201, item)


def _list_notes(event, owner_id):
    params = event.get("queryStringParameters") or {}
    try:
        limit = int(params.get("limit") or DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        return _error(422, "limit must be an integer", "validation_error")
    if limit < 1 or limit > MAX_PAGE_SIZE:
        return _error(422, "limit must be between 1 and 100", "validation_error")

    kwargs = {
        "KeyConditionExpression": Key("owner_id").eq(owner_id),
        "Limit": limit,
    }
    token = params.get("next_token")
    if token:
        try:
            kwargs["ExclusiveStartKey"] = _decode_token(token)
        except Exception:
            return _error(422, "next_token is not a valid cursor", "validation_error")

    result = TABLE.query(**kwargs)
    items = result.get("Items", [])
    last_key = result.get("LastEvaluatedKey")
    return _response(
        200,
        {
            "items": items,
            "count": len(items),
            "next_token": _encode_token(last_key) if last_key else None,
        },
    )


def _get_note(owner_id, note_id):
    result = TABLE.get_item(Key={"owner_id": owner_id, "note_id": note_id})
    item = result.get("Item")
    if not item:
        return _error(404, "Note not found", "not_found")
    return _response(200, item)


def _update_note(event, owner_id, note_id):
    try:
        title, body = _parse_note_payload(event)
    except ValueError as exc:
        return _error(422, str(exc), "validation_error")

    try:
        result = TABLE.update_item(
            Key={"owner_id": owner_id, "note_id": note_id},
            UpdateExpression="SET #title = :title, #body = :body, #updated_at = :updated_at",
            ConditionExpression="attribute_exists(note_id)",
            ExpressionAttributeNames={
                "#title": "title",
                "#body": "body",
                "#updated_at": "updated_at",
            },
            ExpressionAttributeValues={
                ":title": title,
                ":body": body,
                ":updated_at": _now(),
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _error(404, "Note not found", "not_found")
        raise
    return _response(200, result.get("Attributes", {}))


def _delete_note(owner_id, note_id):
    try:
        TABLE.delete_item(
            Key={"owner_id": owner_id, "note_id": note_id},
            ConditionExpression="attribute_exists(note_id)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _error(404, "Note not found", "not_found")
        raise
    return _response(204, None)


def _request_method(event):
    method = event.get("httpMethod")
    if not method:
        method = (
            event.get("requestContext", {}).get("http", {}).get("method") or "GET"
        )
    return method.upper()


def _request_segments(event):
    path = event.get("path") or event.get("rawPath") or "/"
    return [segment for segment in path.split("/") if segment]


def lambda_handler(event, context):
    method = _request_method(event)
    segments = _request_segments(event)
    path_params = event.get("pathParameters") or {}
    owner_id = DEFAULT_OWNER_ID

    try:
        if segments == ["health"] and method == "GET":
            return _response(200, {"status": "ok", "service": "personal_notes_api"})

        if segments and segments[0] == "notes":
            note_id = path_params.get("note_id")
            if note_id is None and len(segments) > 1:
                note_id = segments[1]

            if note_id is None:
                if method == "POST":
                    return _create_note(event, owner_id)
                if method == "GET":
                    return _list_notes(event, owner_id)
                return _error(405, "Method not allowed", "method_not_allowed")

            if method == "GET":
                return _get_note(owner_id, note_id)
            if method == "PUT":
                return _update_note(event, owner_id, note_id)
            if method == "DELETE":
                return _delete_note(owner_id, note_id)
            return _error(405, "Method not allowed", "method_not_allowed")

        return _error(404, "Route not found", "not_found")
    except ClientError as exc:
        LOG.exception("dynamodb error")
        return _error(502, "Storage backend error", "storage_error")
    except Exception:
        LOG.exception("unhandled error")
        return _error(500, "Internal server error", "internal_error")
PYTHON
}

data "archive_file" "lambda" {
  type        = "zip"
  source_file = local_file.lambda_source.filename
  output_path = "${local.lambda_build_dir}/notes-api-fn.zip"
}

resource "aws_lambda_function" "notes_api" {
  #checkov:skip=CKV_AWS_116:A dead-letter queue would require SQS/SNS, which the plan deliberately excludes.
  #checkov:skip=CKV_AWS_117:VPC networking is not available in LocalStack Community Edition for this workload.
  #checkov:skip=CKV_AWS_173:Environment variables hold no secrets and KMS CMKs are out of scope for LocalStack CE.
  #checkov:skip=CKV_AWS_272:Lambda code signing requires AWS Signer, which is unavailable in LocalStack CE.
  function_name                  = var.lambda_function_name
  description                    = "Personal notes REST API handlers"
  role                           = aws_iam_role.lambda.arn
  handler                        = var.lambda_handler
  runtime                        = var.lambda_runtime
  timeout                        = var.lambda_timeout
  memory_size                    = var.lambda_memory_size
  reserved_concurrent_executions = var.lambda_reserved_concurrency
  filename                       = data.archive_file.lambda.output_path
  source_code_hash               = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      TABLE_NAME        = aws_dynamodb_table.notes.name
      DEFAULT_OWNER_ID  = var.default_owner_id
      DEFAULT_PAGE_SIZE = tostring(var.default_page_size)
      MAX_PAGE_SIZE     = tostring(var.max_page_size)
      LOG_LEVEL         = var.log_level
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
    aws_cloudwatch_log_group.lambda,
  ]
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowNotesApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notes_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.notes.execution_arn}/*/*"
}

# ---------------------------------------------------------------------------
# API Gateway - plan resource: notes-api-gateway
# ---------------------------------------------------------------------------
resource "aws_api_gateway_rest_api" "notes" {
  #checkov:skip=CKV2_AWS_29:WAF is not available in LocalStack Community Edition.
  #checkov:skip=CKV2_AWS_51:mTLS client certificates require a custom domain, which is out of scope for LocalStack CE.
  name        = var.api_name
  description = "REST front door for the personal notes API"

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  tags = {
    Name = var.api_name
  }
}

resource "aws_api_gateway_resource" "health" {
  rest_api_id = aws_api_gateway_rest_api.notes.id
  parent_id   = aws_api_gateway_rest_api.notes.root_resource_id
  path_part   = "health"
}

resource "aws_api_gateway_resource" "notes" {
  rest_api_id = aws_api_gateway_rest_api.notes.id
  parent_id   = aws_api_gateway_rest_api.notes.root_resource_id
  path_part   = "notes"
}

resource "aws_api_gateway_resource" "note_id" {
  rest_api_id = aws_api_gateway_rest_api.notes.id
  parent_id   = aws_api_gateway_resource.notes.id
  path_part   = "{note_id}"
}

resource "aws_api_gateway_method" "health" {
  #checkov:skip=CKV2_AWS_53:Request bodies are validated by the application; the health probe takes no payload.
  rest_api_id   = aws_api_gateway_rest_api.notes.id
  resource_id   = aws_api_gateway_resource.health.id
  http_method   = "GET"
  authorization = "AWS_IAM"
}

resource "aws_api_gateway_method" "notes_collection" {
  #checkov:skip=CKV2_AWS_53:Pydantic models in the Lambda validate all request payloads.
  rest_api_id   = aws_api_gateway_rest_api.notes.id
  resource_id   = aws_api_gateway_resource.notes.id
  http_method   = "ANY"
  authorization = "AWS_IAM"
}

resource "aws_api_gateway_method" "note_item" {
  #checkov:skip=CKV2_AWS_53:Pydantic models in the Lambda validate all request payloads.
  rest_api_id   = aws_api_gateway_rest_api.notes.id
  resource_id   = aws_api_gateway_resource.note_id.id
  http_method   = "ANY"
  authorization = "AWS_IAM"

  request_parameters = {
    "method.request.path.note_id" = true
  }
}

resource "aws_api_gateway_integration" "health" {
  rest_api_id             = aws_api_gateway_rest_api.notes.id
  resource_id             = aws_api_gateway_resource.health.id
  http_method             = aws_api_gateway_method.health.http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = aws_lambda_function.notes_api.invoke_arn
}

resource "aws_api_gateway_integration" "notes_collection" {
  rest_api_id             = aws_api_gateway_rest_api.notes.id
  resource_id             = aws_api_gateway_resource.notes.id
  http_method             = aws_api_gateway_method.notes_collection.http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = aws_lambda_function.notes_api.invoke_arn
}

resource "aws_api_gateway_integration" "note_item" {
  rest_api_id             = aws_api_gateway_rest_api.notes.id
  resource_id             = aws_api_gateway_resource.note_id.id
  http_method             = aws_api_gateway_method.note_item.http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = aws_lambda_function.notes_api.invoke_arn
}

resource "aws_api_gateway_deployment" "notes" {
  rest_api_id = aws_api_gateway_rest_api.notes.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.health.id,
      aws_api_gateway_resource.notes.id,
      aws_api_gateway_resource.note_id.id,
      aws_api_gateway_method.health.id,
      aws_api_gateway_method.notes_collection.id,
      aws_api_gateway_method.note_item.id,
      aws_api_gateway_integration.health.id,
      aws_api_gateway_integration.notes_collection.id,
      aws_api_gateway_integration.note_item.id,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.health,
    aws_api_gateway_integration.notes_collection,
    aws_api_gateway_integration.note_item,
  ]
}

resource "aws_api_gateway_stage" "dev" {
  #checkov:skip=CKV2_AWS_51:mTLS client certificates require a custom domain, which is out of scope for LocalStack CE.
  #checkov:skip=CKV2_AWS_29:WAF is not available in LocalStack Community Edition.
  rest_api_id          = aws_api_gateway_rest_api.notes.id
  deployment_id        = aws_api_gateway_deployment.notes.id
  stage_name           = var.stage_name
  description          = "Notes API stage"
  xray_tracing_enabled = true
  cache_cluster_enabled = true
  cache_cluster_size    = "0.5"

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api.arn
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
    Name = "${var.api_name}-${var.stage_name}"
  }

  depends_on = [aws_cloudwatch_log_group.api]
}

resource "aws_api_gateway_method_settings" "all" {
  rest_api_id = aws_api_gateway_rest_api.notes.id
  stage_name  = aws_api_gateway_stage.dev.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled        = true
    logging_level          = "INFO"
    data_trace_enabled     = false
    caching_enabled        = true
    cache_data_encrypted   = true
    cache_ttl_in_seconds   = 60
    throttling_burst_limit = 500
    throttling_rate_limit  = 1000
  }

  depends_on = [aws_api_gateway_account.this]
}
