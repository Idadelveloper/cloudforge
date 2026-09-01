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

# ---------------------------------------------------------------------------
# Customer managed KMS key
# Supports encryption-at-rest requirements for the DynamoDB table (expenses)
# and the CloudWatch log group (/cloudforge/expense-tracker-api).
# ---------------------------------------------------------------------------
resource "aws_kms_key" "main" {
  description             = "CMK for ${var.project_name} DynamoDB table and CloudWatch logs"
  deletion_window_in_days = var.kms_key_deletion_window_days
  enable_key_rotation     = true
  is_enabled              = true

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
        Sid    = "AllowCloudWatchLogsUseOfTheKey"
        Effect = "Allow"
        Principal = {
          Service = "logs.${data.aws_region.current.name}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*"
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:*"
          }
        }
      },
      {
        Sid    = "AllowExpenseServiceUseOfTheKey"
        Effect = "Allow"
        Principal = {
          Service = "dynamodb.amazonaws.com"
        }
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

  tags = {
    Name = "${var.project_name}-cmk"
  }
}

resource "aws_kms_alias" "main" {
  name          = var.kms_key_alias
  target_key_id = aws_kms_key.main.key_id
}

# ---------------------------------------------------------------------------
# DynamoDB: expenses table + month-date-index + category-date-index
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "expenses" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "user_id"
  range_key    = "expense_id"

  attribute {
    name = "user_id"
    type = "S"
  }

  attribute {
    name = "expense_id"
    type = "S"
  }

  attribute {
    name = "month"
    type = "S"
  }

  attribute {
    name = "category"
    type = "S"
  }

  attribute {
    name = "date"
    type = "S"
  }

  global_secondary_index {
    name            = var.month_index_name
    hash_key        = "month"
    range_key       = "date"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = var.category_index_name
    hash_key        = "category"
    range_key       = "date"
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
    Name = var.dynamodb_table_name
  }
}

# ---------------------------------------------------------------------------
# CloudWatch Logs: application log group
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "app" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn

  tags = {
    Name = var.log_group_name
  }
}

# ---------------------------------------------------------------------------
# IAM: least-privilege role + policy for the expense tracker service
# ---------------------------------------------------------------------------
resource "aws_iam_role" "app" {
  name        = var.iam_role_name
  description = "Role assumed by the ${var.project_name} service process"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowComputeServiceToAssumeRole"
        Effect = "Allow"
        Principal = {
          Service = [
            "ec2.amazonaws.com",
            "ecs-tasks.amazonaws.com"
          ]
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = var.iam_role_name
  }
}

resource "aws_iam_policy" "app" {
  name        = var.iam_policy_name
  description = "Least-privilege access to the expenses table, its indexes, the application log group and the encryption key"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ExpensesTableItemAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:BatchGetItem",
          "dynamodb:DescribeTable"
        ]
        Resource = [
          aws_dynamodb_table.expenses.arn
        ]
      },
      {
        Sid    = "ExpensesIndexQueryAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:Query"
        ]
        Resource = [
          "${aws_dynamodb_table.expenses.arn}/index/${var.month_index_name}",
          "${aws_dynamodb_table.expenses.arn}/index/${var.category_index_name}"
        ]
      },
      {
        Sid    = "ApplicationLogWrite"
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
      },
      {
        Sid    = "EncryptionKeyUsage"
        Effect = "Allow"
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = [
          aws_kms_key.main.arn
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
