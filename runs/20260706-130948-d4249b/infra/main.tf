data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# KMS key used to encrypt DynamoDB, SQS DLQ, Lambda env vars and log groups.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "main" {
  description             = "CMK for personal_notes_api resources"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableRootPermissions"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AllowCloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${data.aws_region.current.name}.amazonaws.com" }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_kms_alias" "main" {
  name          = "alias/personal-notes-api"
  target_key_id = aws_kms_key.main.key_id
}

# ---------------------------------------------------------------------------
# DynamoDB table for notes
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "notes" {
  name         = var.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "note_id"

  attribute {
    name = "note_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.main.arn
  }
}

# ---------------------------------------------------------------------------
# SQS dead-letter queue for the Lambda function
# ---------------------------------------------------------------------------
resource "aws_sqs_queue" "lambda_dlq" {
  name                              = "${var.lambda_function_name}-dlq"
  kms_master_key_id                 = aws_kms_key.main.arn
  kms_data_key_reuse_period_seconds = 300
  message_retention_seconds         = 1209600
}

# ---------------------------------------------------------------------------
# CloudWatch log groups
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.lambda_function_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_cloudwatch_log_group" "apigw" {
  name              = "/aws/apigateway/${var.api_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

# ---------------------------------------------------------------------------
# IAM role and least-privilege policy for the Lambda function
# ---------------------------------------------------------------------------
resource "aws_iam_role" "lambda" {
  name = var.iam_role_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { Service = "lambda.amazonaws.com" }
      }
    ]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${var.iam_role_name}-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDbAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Scan",
          "dynamodb:Query",
          "dynamodb:BatchGetItem",
          "dynamodb:BatchWriteItem"
        ]
        Resource = [
          aws_dynamodb_table.notes.arn,
          "${aws_dynamodb_table.notes.arn}/index/*"
        ]
      },
      {
        Sid    = "LogsAccess"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.lambda.arn}:*"
      },
      {
        Sid      = "DlqAccess"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.lambda_dlq.arn
      },
      {
        Sid    = "KmsAccess"
        Effect = "Allow"
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = aws_kms_key.main.arn
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# Lambda source packaging (inline code, no pre-built artifact required)
# ---------------------------------------------------------------------------
resource "local_file" "lambda_code" {
  filename = "${path.module}/build/lambda_function.py"
  content  = <<-PYCODE
    import json
    import os
    import uuid
    import datetime
    import boto3

    dynamodb = boto3.resource("dynamodb")
    TABLE_NAME = os.environ.get("TABLE_NAME", "notes")
    table = dynamodb.Table(TABLE_NAME)


    def _now():
        return datetime.datetime.utcnow().isoformat() + "Z"


    def _response(status, body):
        return {
            "statusCode": status,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body),
        }


    def handler(event, context):
        method = event.get("httpMethod", "GET")
        path_params = event.get("pathParameters") or {}
        note_id = path_params.get("proxy") or path_params.get("note_id")
        raw_body = event.get("body") or "{}"
        try:
            payload = json.loads(raw_body)
        except (ValueError, TypeError):
            payload = {}

        if method == "POST":
            item = {
                "note_id": str(uuid.uuid4()),
                "title": payload.get("title", ""),
                "body": payload.get("body", ""),
                "created_at": _now(),
                "updated_at": _now(),
            }
            table.put_item(Item=item)
            return _response(201, item)

        if method == "GET" and not note_id:
            result = table.scan()
            return _response(200, result.get("Items", []))

        if method == "GET" and note_id:
            result = table.get_item(Key={"note_id": note_id})
            item = result.get("Item")
            if not item:
                return _response(404, {"message": "Not found"})
            return _response(200, item)

        if method == "PUT" and note_id:
            result = table.get_item(Key={"note_id": note_id})
            item = result.get("Item")
            if not item:
                return _response(404, {"message": "Not found"})
            item["title"] = payload.get("title", item["title"])
            item["body"] = payload.get("body", item["body"])
            item["updated_at"] = _now()
            table.put_item(Item=item)
            return _response(200, item)

        if method == "DELETE" and note_id:
            table.delete_item(Key={"note_id": note_id})
            return _response(204, {})

        return _response(400, {"message": "Unsupported operation"})
  PYCODE
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = local_file.lambda_code.filename
  output_path = "${path.module}/build/lambda.zip"
}

# ---------------------------------------------------------------------------
# Lambda function hosting the API
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "api" {
  # checkov:skip=CKV_AWS_50:X-Ray tracing is not supported by LocalStack Community
  # checkov:skip=CKV_AWS_117:VPC deployment is not required for LocalStack Community
  # checkov:skip=CKV_AWS_272:Code signing is not supported by LocalStack Community
  function_name = var.lambda_function_name
  role          = aws_iam_role.lambda.arn
  handler       = "lambda_function.handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 256

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  reserved_concurrent_executions = var.lambda_reserved_concurrency

  kms_key_arn = aws_kms_key.main.arn

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.notes.name
    }
  }

  dead_letter_config {
    target_arn = aws_sqs_queue.lambda_dlq.arn
  }

  depends_on = [
    aws_iam_role_policy.lambda,
    aws_cloudwatch_log_group.lambda
  ]
}

# ---------------------------------------------------------------------------
# API Gateway REST API (Lambda proxy integration)
# ---------------------------------------------------------------------------
resource "aws_api_gateway_rest_api" "api" {
  # checkov:skip=CKV2_AWS_29:WAF is not supported by LocalStack Community
  # checkov:skip=CKV2_AWS_51:Client certificate authentication not used for this API
  name = var.api_name

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_resource" "proxy" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_method" "proxy" {
  # checkov:skip=CKV_AWS_59:Public notes API intentionally exposes endpoints without an authorizer
  # checkov:skip=CKV2_AWS_53:Request validation handled within the application layer
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.proxy.id
  http_method   = "ANY"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "proxy" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.proxy.id
  http_method             = aws_api_gateway_method.proxy.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

resource "aws_api_gateway_method" "root" {
  # checkov:skip=CKV_AWS_59:Public notes API intentionally exposes endpoints without an authorizer
  # checkov:skip=CKV2_AWS_53:Request validation handled within the application layer
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_rest_api.api.root_resource_id
  http_method   = "ANY"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "root" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_rest_api.api.root_resource_id
  http_method             = aws_api_gateway_method.root.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

resource "aws_api_gateway_deployment" "api" {
  rest_api_id = aws_api_gateway_rest_api.api.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.proxy.id,
      aws_api_gateway_method.proxy.id,
      aws_api_gateway_integration.proxy.id,
      aws_api_gateway_method.root.id,
      aws_api_gateway_integration.root.id,
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

resource "aws_api_gateway_stage" "api" {
  # checkov:skip=CKV_AWS_120:API Gateway caching is not supported by LocalStack Community
  # checkov:skip=CKV2_AWS_29:WAF is not supported by LocalStack Community
  # checkov:skip=CKV2_AWS_51:Client certificate authentication not used for this API
  rest_api_id           = aws_api_gateway_rest_api.api.id
  deployment_id         = aws_api_gateway_deployment.api.id
  stage_name            = var.stage_name
  xray_tracing_enabled  = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      resourcePath   = "$context.resourcePath"
      status         = "$context.status"
      responseLength = "$context.responseLength"
    })
  }
}

resource "aws_api_gateway_method_settings" "api" {
  # checkov:skip=CKV_AWS_225:API Gateway cache encryption not applicable without caching on LocalStack
  rest_api_id = aws_api_gateway_rest_api.api.id
  stage_name  = aws_api_gateway_stage.api.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled = true
    logging_level   = "INFO"
  }
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}
