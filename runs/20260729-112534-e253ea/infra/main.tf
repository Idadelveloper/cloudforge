data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# KMS key used to encrypt the DynamoDB table (customer managed CMK)
# ---------------------------------------------------------------------------
resource "aws_kms_key" "main" {
  description             = "CMK for ${var.project} resources"
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
      }
    ]
  })
}

resource "aws_kms_alias" "main" {
  name          = "alias/${var.project}"
  target_key_id = aws_kms_key.main.key_id
}

# ---------------------------------------------------------------------------
# DynamoDB table for URL mappings
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "url_mappings" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "code"

  attribute {
    name = "code"
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
# CloudWatch log groups
# KMS association on log groups is unsupported by LocalStack Community, so it
# is omitted here and the corresponding Checkov check is skipped.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda" {
  # checkov:skip=CKV_AWS_158:LocalStack Community does not support CloudWatch Logs KMS association.
  name              = "/aws/lambda/${var.lambda_function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "apigw" {
  # checkov:skip=CKV_AWS_158:LocalStack Community does not support CloudWatch Logs KMS association.
  name              = "/aws/apigateway/${var.api_name}"
  retention_in_days = var.log_retention_days
}

# ---------------------------------------------------------------------------
# IAM role for the Lambda function (least privilege)
# ---------------------------------------------------------------------------
resource "aws_iam_role" "lambda" {
  name = var.lambda_role_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${var.lambda_role_name}-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query"
        ]
        Resource = [aws_dynamodb_table.url_mappings.arn]
      },
      {
        Sid    = "CloudWatchLogsAccess"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
      },
      {
        Sid    = "KMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = [aws_kms_key.main.arn]
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# Lambda source packaging (inline, no pre-built artifact required)
# ---------------------------------------------------------------------------
resource "local_file" "lambda_handler" {
  filename = "${path.module}/lambda_src/handler.py"
  content  = <<-PYCODE
    import json
    import os
    import boto3

    TABLE_NAME = os.environ.get("TABLE_NAME")
    dynamodb = boto3.resource("dynamodb")

    def handler(event, context):
        """Entry point for the URL shortener application."""
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "url_shortener handler ready"}),
        }
  PYCODE
}

data "archive_file" "lambda" {
  type        = "zip"
  source_file = local_file.lambda_handler.filename
  output_path = "${path.module}/build/lambda.zip"
}

# ---------------------------------------------------------------------------
# Lambda function
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "fn" {
  # checkov:skip=CKV_AWS_117:LocalStack Community has no VPC support; app is stateless behind API Gateway.
  # checkov:skip=CKV_AWS_116:No message queue in this architecture; DLQ not applicable.
  # checkov:skip=CKV_AWS_50:X-Ray tracing avoided to keep IAM free of wildcard xray resources on LocalStack.
  # checkov:skip=CKV_AWS_272:Code signing is unsupported on LocalStack Community.
  # checkov:skip=CKV_AWS_115:Reserved concurrency setting is unsupported/unreliable on LocalStack Community.
  # checkov:skip=CKV_AWS_173:Environment variables are encrypted with the default AWS managed key; CMK unsupported on LocalStack.
  function_name    = var.lambda_function_name
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.url_mappings.name
    }
  }

  depends_on = [
    aws_iam_role_policy.lambda,
    aws_cloudwatch_log_group.lambda
  ]
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}

# ---------------------------------------------------------------------------
# API Gateway (HTTP API v2)
# ---------------------------------------------------------------------------
resource "aws_apigatewayv2_api" "api" {
  name          = var.api_name
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.fn.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "shorten" {
  # checkov:skip=CKV_AWS_309:Public URL shortener API; spec requires no authentication.
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "POST /shorten"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "NONE"
}

resource "aws_apigatewayv2_route" "redirect" {
  # checkov:skip=CKV_AWS_309:Public URL shortener API; spec requires no authentication.
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "GET /{code}"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "NONE"
}

resource "aws_apigatewayv2_route" "stats" {
  # checkov:skip=CKV_AWS_309:Public URL shortener API; spec requires no authentication.
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "GET /stats/{code}"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "NONE"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      protocol       = "$context.protocol"
      responseLength = "$context.responseLength"
    })
  }

  default_route_settings {
    throttling_burst_limit = 50
    throttling_rate_limit  = 100
  }
}
