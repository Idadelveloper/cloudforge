##############################################
# DynamoDB: bookmarks table + tag GSI
##############################################

resource "aws_dynamodb_table" "bookmarks" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "bookmark_id"

  attribute {
    name = "bookmark_id"
    type = "S"
  }

  attribute {
    name = "tag"
    type = "S"
  }

  global_secondary_index {
    name            = var.dynamodb_tag_index_name
    hash_key        = "tag"
    range_key       = "bookmark_id"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = {
    Name        = var.dynamodb_table_name
    environment = var.environment
    component   = "bookmark-store"
  }
}

##############################################
# Secrets Manager: shared API key
##############################################

resource "aws_secretsmanager_secret" "api_key" {
  #checkov:skip=CKV_AWS_149:LocalStack Community Edition does not provide customer managed KMS keys for this stack; the AWS managed key is used.
  #checkov:skip=CKV2_AWS_57:The shared API key is rotated out of band by redeploying the stack; no rotation Lambda is available in this environment.
  name                    = var.api_key_secret_name
  description             = "Shared API key validated against the X-API-Key header of the bookmark manager API."
  recovery_window_in_days = 0

  tags = {
    Name        = var.api_key_secret_name
    environment = var.environment
    component   = "api-auth"
  }
}

resource "aws_secretsmanager_secret_version" "api_key" {
  secret_id = aws_secretsmanager_secret.api_key.id

  secret_string = jsonencode({
    api_key = var.api_key
  })
}

##############################################
# CloudWatch Logs: application log group
##############################################

resource "aws_cloudwatch_log_group" "app" {
  #checkov:skip=CKV_AWS_158:LocalStack Community Edition does not provide customer managed KMS keys; logs use the default CloudWatch encryption.
  name              = var.log_group_name
  retention_in_days = var.log_retention_in_days

  tags = {
    Name        = var.log_group_name
    environment = var.environment
    component   = "observability"
  }
}

##############################################
# IAM: application role and least-privilege policy
##############################################

data "aws_iam_policy_document" "app_assume_role" {
  statement {
    sid     = "AllowServiceRuntimeAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name               = var.app_role_name
  description        = "Runtime role for the bookmark manager API service."
  assume_role_policy = data.aws_iam_policy_document.app_assume_role.json

  tags = {
    Name        = var.app_role_name
    environment = var.environment
    component   = "iam"
  }
}

data "aws_iam_policy_document" "app" {
  statement {
    sid    = "BookmarksTableAccess"
    effect = "Allow"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:DeleteItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:DescribeTable",
      "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem",
    ]

    resources = [
      aws_dynamodb_table.bookmarks.arn,
      "${aws_dynamodb_table.bookmarks.arn}/index/${var.dynamodb_tag_index_name}",
    ]
  }

  statement {
    sid    = "ApiKeySecretRead"
    effect = "Allow"

    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
    ]

    resources = [
      aws_secretsmanager_secret.api_key.arn,
    ]
  }

  statement {
    sid    = "ApplicationLogWrite"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]

    resources = [
      aws_cloudwatch_log_group.app.arn,
      "${aws_cloudwatch_log_group.app.arn}:log-stream:*",
    ]
  }
}

resource "aws_iam_policy" "app" {
  name        = var.app_policy_name
  description = "Least-privilege access to the bookmarks table, API key secret and application log group."
  policy      = data.aws_iam_policy_document.app.json

  tags = {
    Name        = var.app_policy_name
    environment = var.environment
    component   = "iam"
  }
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
