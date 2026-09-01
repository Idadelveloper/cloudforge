data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  region     = data.aws_region.current.name

  application_log_group_arn = "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:${var.application_log_group_name}"
}

#############################################
# Customer managed KMS key
# Supports the DynamoDB products table (CMK SSE)
# and the application CloudWatch log group.
#############################################

resource "aws_kms_key" "inventory" {
  description             = "Customer managed key for the shop inventory products table and application logs"
  deletion_window_in_days = var.kms_key_deletion_window_in_days
  enable_key_rotation     = true
  is_enabled              = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AccountKeyAdministration"
        Effect = "Allow"
        Principal = {
          AWS = "arn:${local.partition}:iam::${local.account_id}:root"
        }
        Action = [
          "kms:Create*",
          "kms:Describe*",
          "kms:Enable*",
          "kms:List*",
          "kms:Put*",
          "kms:Update*",
          "kms:Revoke*",
          "kms:Disable*",
          "kms:Get*",
          "kms:Delete*",
          "kms:TagResource",
          "kms:UntagResource",
          "kms:ScheduleKeyDeletion",
          "kms:CancelKeyDeletion"
        ]
        Resource = "arn:${local.partition}:kms:${local.region}:${local.account_id}:key/*"
      },
      {
        Sid    = "AllowApplicationRoleDataKeyUse"
        Effect = "Allow"
        Principal = {
          AWS = "arn:${local.partition}:iam::${local.account_id}:role/${var.app_role_name}"
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
        Resource = "arn:${local.partition}:kms:${local.region}:${local.account_id}:key/*"
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
        Resource = "arn:${local.partition}:kms:${local.region}:${local.account_id}:key/*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:*"
          }
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-cmk"
  }
}

resource "aws_kms_alias" "inventory" {
  name          = var.kms_key_alias
  target_key_id = aws_kms_key.inventory.key_id
}

#############################################
# DynamoDB: shop-inventory-products
# Primary datastore for product records.
#############################################

resource "aws_dynamodb_table" "products" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = var.dynamodb_hash_key

  attribute {
    name = var.dynamodb_hash_key
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.inventory.arn
  }

  deletion_protection_enabled = false

  ttl {
    attribute_name = ""
    enabled        = false
  }

  tags = {
    Name = var.dynamodb_table_name
  }
}

#############################################
# CloudWatch Logs: /shop-inventory-api/application
#############################################

resource "aws_cloudwatch_log_group" "application" {
  name              = var.application_log_group_name
  retention_in_days = var.log_retention_in_days
  kms_key_id        = aws_kms_key.inventory.arn

  tags = {
    Name = var.application_log_group_name
  }
}

#############################################
# IAM: application execution role + policy
#############################################

resource "aws_iam_role" "app" {
  name        = var.app_role_name
  description = "Execution identity for the shop inventory FastAPI service"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowEc2HostedServiceToAssume"
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

resource "aws_iam_policy" "app_dynamodb" {
  name        = var.app_policy_name
  description = "Least-privilege access to the shop inventory products table and application log group"

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
          "dynamodb:DescribeTable"
        ]
        Resource = aws_dynamodb_table.products.arn
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
          aws_cloudwatch_log_group.application.arn,
          "${local.application_log_group_arn}:log-stream:*"
        ]
      },
      {
        Sid    = "EncryptDecryptWithInventoryKey"
        Effect = "Allow"
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey"
        ]
        Resource = aws_kms_key.inventory.arn
      }
    ]
  })

  tags = {
    Name = var.app_policy_name
  }
}

resource "aws_iam_role_policy_attachment" "app_dynamodb" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app_dynamodb.arn
}
