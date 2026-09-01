provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project      = var.project_name
      "managed-by" = "cloudforge-terraform"
      environment  = var.environment
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
  api_key    = var.api_key_value != "" ? var.api_key_value : random_password.api_key.result
}

# ---------------------------------------------------------------------------
# Customer managed KMS key: encrypts the DynamoDB tables, the API key secret
# and the application log group.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "main" {
  description             = "CMK for ${var.project_name} DynamoDB tables, secret and logs"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  is_enabled              = true

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
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${local.region}:${local.account_id}:log-group:${var.log_group_name}"
          }
        }
      }
    ]
  })
}

resource "aws_kms_alias" "main" {
  name          = "alias/${var.project_name}"
  target_key_id = aws_kms_key.main.key_id
}

# ---------------------------------------------------------------------------
# DynamoDB: primary bookmark store
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "bookmarks" {
  name         = var.bookmarks_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "bookmark_id"

  attribute {
    name = "bookmark_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.main.arn
  }

  tags = {
    Name = var.bookmarks_table_name
    role = "bookmark-primary-store"
  }
}

# ---------------------------------------------------------------------------
# DynamoDB: tag lookup table (one row per bookmark/tag pair)
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "bookmark_tags" {
  name         = var.bookmark_tags_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "tag"
  range_key    = "bookmark_id"

  attribute {
    name = "tag"
    type = "S"
  }

  attribute {
    name = "bookmark_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = var.tag_created_at_index_name
    hash_key        = "tag"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.main.arn
  }

  tags = {
    Name = var.bookmark_tags_table_name
    role = "bookmark-tag-index"
  }
}

# ---------------------------------------------------------------------------
# Secrets Manager: shared API key checked against the X-API-Key header
# ---------------------------------------------------------------------------
resource "random_password" "api_key" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "api_key" {
  #checkov:skip=CKV2_AWS_57:Static shared API key deployed to LocalStack Community; automatic rotation requires a rotation Lambda which is out of scope for this plan.
  name                    = var.api_key_secret_name
  description             = "Shared API key validated on every request via the X-API-Key header"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 0

  tags = {
    Name = var.api_key_secret_name
    role = "api-authentication"
  }
}

resource "aws_secretsmanager_secret_version" "api_key" {
  secret_id = aws_secretsmanager_secret.api_key.id

  secret_string = jsonencode({
    api_key = local.api_key
  })
}

# ---------------------------------------------------------------------------
# CloudWatch Logs: application request/error logs
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "app" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn

  tags = {
    Name = var.log_group_name
    role = "application-logs"
  }

  depends_on = [aws_kms_key.main]
}

# ---------------------------------------------------------------------------
# IAM: least privilege role for the bookmark manager service
# ---------------------------------------------------------------------------
resource "aws_iam_role" "app" {
  name        = var.app_role_name
  description = "Least privilege role for the ${var.project_name} service"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowComputeServiceAssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = var.app_role_name
  }
}

resource "aws_iam_policy" "app" {
  name        = "${var.app_role_name}-policy"
  description = "DynamoDB, Secrets Manager, KMS and CloudWatch Logs access scoped to ${var.project_name} resources"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BookmarkTableAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:BatchGetItem",
          "dynamodb:BatchWriteItem",
          "dynamodb:DescribeTable"
        ]
        Resource = [
          aws_dynamodb_table.bookmarks.arn,
          aws_dynamodb_table.bookmark_tags.arn,
          "${aws_dynamodb_table.bookmark_tags.arn}/index/${var.tag_created_at_index_name}"
        ]
      },
      {
        Sid    = "ApiKeySecretRead"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ]
        Resource = [
          aws_secretsmanager_secret.api_key.arn
        ]
      },
      {
        Sid    = "EncryptionKeyUse"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey"
        ]
        Resource = [
          aws_kms_key.main.arn
        ]
      },
      {
        Sid    = "ApplicationLogging"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = [
          aws_cloudwatch_log_group.app.arn,
          "${aws_cloudwatch_log_group.app.arn}:log-stream:*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
