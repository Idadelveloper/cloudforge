# ---------------------------------------------------------------------------
# DynamoDB table: notes
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "notes" {
  name         = var.dynamodb_table_name
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
    enabled = true
  }
}

# ---------------------------------------------------------------------------
# IAM role for the Lambda function (least privilege)
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_role" {
  name               = var.lambda_role_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "lambda_policy" {
  statement {
    sid    = "NotesTableAccess"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan"
    ]
    resources = [
      aws_dynamodb_table.notes.arn,
      "${aws_dynamodb_table.notes.arn}/index/*"
    ]
  }

  statement {
    sid    = "LambdaLogging"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]
    resources = [
      "${aws_cloudwatch_log_group.lambda_logs.arn}:*"
    ]
  }

  statement {
    sid    = "XRayTracing"
    effect = "Allow"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords"
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "lambda_policy" {
  name   = "${var.lambda_role_name}-policy"
  role   = aws_iam_role.lambda_role.id
  policy = data.aws_iam_policy_document.lambda_policy.json
}

# ---------------------------------------------------------------------------
# CloudWatch log groups
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda_logs" {
  name              = "/aws/lambda/${var.lambda_function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "apigw_logs" {
  name              = "/aws/apigateway/${var.api_gateway_name}"
  retention_in_days = var.log_retention_days
}

# ---------------------------------------------------------------------------
# Lambda source packaging (inline code, no pre-built artifact)
# ---------------------------------------------------------------------------
resource "local_file" "lambda_source" {
  filename = "${path.module}/lambda_src/main.py"
  content  = <<-PYEOF
    import json
    import os
    import uuid
    from datetime import datetime, timezone

    import boto3

    TABLE_NAME = os.environ.get("NOTES_TABLE", "notes")
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(TABLE_NAME)


    def _now():
        return datetime.now(timezone.utc).isoformat()


    def _response(status, body):
        return {
            "statusCode": status,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body),
        }


    def handler(event, context):
        method = event.get("httpMethod", "GET")
        path_params = event.get("pathParameters") or {}
        note_id = path_params.get("note_id")
        raw_body = event.get("body") or "{}"
        try:
            payload = json.loads(raw_body)
        except (ValueError, TypeError):
            payload = {}

        if method == "POST" and not note_id:
            new_id = str(uuid.uuid4())
            now = _now()
            item = {
                "note_id": new_id,
                "title": payload.get("title", ""),
                "body": payload.get("body", ""),
                "created_at": now,
                "updated_at": now,
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
                return _response(404, {"detail": "Note not found"})
            return _response(200, item)

        if method == "PUT" and note_id:
            result = table.get_item(Key={"note_id": note_id})
            item = result.get("Item")
            if not item:
                return _response(404, {"detail": "Note not found"})
            item["title"] = payload.get("title", item["title"])
            item["body"] = payload.get("body", item["body"])
            item["updated_at"] = _now()
            table.put_item(Item=item)
            return _response(200, item)

        if method == "DELETE" and note_id:
            table.delete_item(Key={"note_id": note_id})
            return _response(204, {})

        return _response(400, {"detail": "Unsupported operation"})
  PYEOF
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = local_file.lambda_source.filename
  output_path = "${path.module}/lambda_function.zip"

  depends_on = [local_file.lambda_source]
}

# ---------------------------------------------------------------------------
# Lambda function
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "handler" {
  function_name = var.lambda_function_name
  role          = aws_iam_role.lambda_role.arn
  handler       = "main.handler"
  runtime       = "python3.12"

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  timeout                        = 30
  memory_size                    = 256
  reserved_concurrent_executions = 10

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      NOTES_TABLE = aws_dynamodb_table.notes.name
    }
  }

  depends_on = [
    aws_iam_role_policy.lambda_policy,
    aws_cloudwatch_log_group.lambda_logs
  ]
}

# ---------------------------------------------------------------------------
# API Gateway (REST) with proxy integration to the Lambda
# ---------------------------------------------------------------------------
resource "aws_api_gateway_rest_api" "api" {
  name        = var.api_gateway_name
  description = "REST API for personal notes."

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_resource" "proxy" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_method" "proxy_any" {
  rest_api_id      = aws_api_gateway_rest_api.api.id
  resource_id      = aws_api_gateway_resource.proxy.id
  http_method      = "ANY"
  authorization    = "NONE"
  api_key_required = false
}

resource "aws_api_gateway_integration" "proxy_lambda" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.proxy.id
  http_method             = aws_api_gateway_method.proxy_any.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.handler.invoke_arn
}

resource "aws_api_gateway_method" "root_any" {
  rest_api_id      = aws_api_gateway_rest_api.api.id
  resource_id      = aws_api_gateway_rest_api.api.root_resource_id
  http_method      = "ANY"
  authorization    = "NONE"
  api_key_required = false
}

resource "aws_api_gateway_integration" "root_lambda" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_rest_api.api.root_resource_id
  http_method             = aws_api_gateway_method.root_any.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.handler.invoke_arn
}

resource "aws_api_gateway_deployment" "deploy" {
  rest_api_id = aws_api_gateway_rest_api.api.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.proxy.id,
      aws_api_gateway_method.proxy_any.id,
      aws_api_gateway_integration.proxy_lambda.id,
      aws_api_gateway_method.root_any.id,
      aws_api_gateway_integration.root_lambda.id
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.proxy_lambda,
    aws_api_gateway_integration.root_lambda
  ]
}

resource "aws_api_gateway_stage" "stage" {
  deployment_id        = aws_api_gateway_deployment.deploy.id
  rest_api_id          = aws_api_gateway_rest_api.api.id
  stage_name           = var.api_stage_name
  xray_tracing_enabled = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw_logs.arn
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
}

resource "aws_api_gateway_method_settings" "all" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  stage_name  = aws_api_gateway_stage.stage.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled = true
    logging_level   = "INFO"
  }
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}
