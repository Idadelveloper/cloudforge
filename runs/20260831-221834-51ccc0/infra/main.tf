provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project      = var.project_name
      "managed-by" = "terraform"
      environment  = var.environment
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

############################################
# Customer managed KMS key
# Encrypts the DynamoDB products table and the API CloudWatch log group.
############################################
resource "aws_kms_key" "inventory" {
  description             = "Customer managed key for the shop inventory API DynamoDB table and log group"
  enable_key_rotation     = true
  deletion_window_in_days = var.kms_key_deletion_window_days
  is_enabled              = true

  policy = jsonencode({
    Version = "2012-10-17"
    Id      = "shop-inventory-api-key-policy"
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
          "kms:ReEncryptFrom",
          "kms:ReEncryptTo",
          "kms:GenerateDataKey",
          "kms:GenerateDataKeyWithoutPlaintext",
          "kms:DescribeKey"
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:${var.log_group_name}"
          }
        }
      },
      {
        Sid    = "AllowInventoryApiRoleUseOfTheKey"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.api_role_name}"
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
    Name = "${var.project_name}-kms"
  }
}

resource "aws_kms_alias" "inventory" {
  name          = var.kms_key_alias
  target_key_id = aws_kms_key.inventory.key_id
}

############################################
# DynamoDB: product catalogue keyed by SKU
# Backs POST /products, GET /products, GET /products/{sku},
# PATCH /products/{sku} and POST /products/{sku}/adjust-stock.
############################################
resource "aws_dynamodb_table" "products" {
  name         = var.products_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "sku"

  attribute {
    name = "sku"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.inventory.arn
  }

  tags = {
    Name = var.products_table_name
  }
}

############################################
# CloudWatch Logs: structured API request/error log group
############################################
resource "aws_cloudwatch_log_group" "api" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.inventory.arn

  tags = {
    Name = var.log_group_name
  }
}

############################################
# IAM: execution identity for the FastAPI inventory service
############################################
data "aws_iam_policy_document" "api_assume_role" {
  statement {
    sid     = "AllowComputeHostToAssumeInventoryApiRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api" {
  name                 = var.api_role_name
  description          = "Execution identity for the shop inventory FastAPI service"
  assume_role_policy   = data.aws_iam_policy_document.api_assume_role.json
  max_session_duration = 3600

  tags = {
    Name = var.api_role_name
  }
}

############################################
# IAM: least-privilege access policy
# Exactly the DynamoDB and CloudWatch Logs calls the application makes.
############################################
resource "aws_iam_policy" "dynamodb_access" {
  name        = var.dynamodb_access_policy_name
  description = "Least-privilege access to the shop inventory products table and API log group"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ProductsTableDataAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:UpdateItem",
          "dynamodb:Scan",
          "dynamodb:Query",
          "dynamodb:DescribeTable"
        ]
        Resource = [
          aws_dynamodb_table.products.arn
        ]
      },
      {
        Sid    = "ApiLogStreamWrite"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = [
          aws_cloudwatch_log_group.api.arn,
          "${aws_cloudwatch_log_group.api.arn}:log-stream:*"
        ]
      },
      {
        Sid    = "TableEncryptionKeyUse"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey"
        ]
        Resource = [
          aws_kms_key.inventory.arn
        ]
      }
    ]
  })

  tags = {
    Name = var.dynamodb_access_policy_name
  }
}

resource "aws_iam_role_policy_attachment" "api_dynamodb_access" {
  role       = aws_iam_role.api.name
  policy_arn = aws_iam_policy.dynamodb_access.arn
}
