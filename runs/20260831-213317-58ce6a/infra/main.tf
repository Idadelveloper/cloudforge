data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

#############################################
# Customer managed KMS key
# Used to encrypt the DynamoDB products table
# and the application CloudWatch log group.
#############################################
resource "aws_kms_key" "inventory" {
  description             = "Customer managed key for ${var.project_name} DynamoDB table and application logs"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  is_enabled              = true
  key_usage               = "ENCRYPT_DECRYPT"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableAccountKeyAdministration"
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
          ArnEquals = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:${var.log_group_name}"
          }
        }
      },
      {
        Sid    = "AllowInventoryServiceRoleUseOfTheKey"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.role_name}"
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
    Name = "${var.project_name}-key"
  }
}

resource "aws_kms_alias" "inventory" {
  name          = var.kms_key_alias
  target_key_id = aws_kms_key.inventory.key_id
}

#############################################
# DynamoDB: products table
# Partition key product_id, GSI on sku.
#############################################
resource "aws_dynamodb_table" "products" {
  name         = var.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "product_id"

  attribute {
    name = "product_id"
    type = "S"
  }

  attribute {
    name = "sku"
    type = "S"
  }

  global_secondary_index {
    name            = var.sku_index_name
    hash_key        = "sku"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.inventory.arn
  }

  deletion_protection_enabled = false

  tags = {
    Name = var.table_name
  }
}

#############################################
# CloudWatch Logs: application log group
#############################################
resource "aws_cloudwatch_log_group" "application" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.inventory.arn

  tags = {
    Name = var.log_group_name
  }
}

#############################################
# IAM: execution role for the inventory API
#############################################
data "aws_iam_policy_document" "assume_role" {
  statement {
    sid     = "AllowComputeServiceToAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "inventory_api" {
  name                 = var.role_name
  description          = "Execution identity for the ${var.project_name} REST service"
  assume_role_policy   = data.aws_iam_policy_document.assume_role.json
  max_session_duration = 3600

  tags = {
    Name = var.role_name
  }
}

data "aws_iam_policy_document" "inventory_api" {
  statement {
    sid    = "ProductsTableDataAccess"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:DescribeTable"
    ]

    resources = [
      aws_dynamodb_table.products.arn,
      "${aws_dynamodb_table.products.arn}/index/${var.sku_index_name}"
    ]
  }

  statement {
    sid    = "ApplicationLogWrite"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams"
    ]

    resources = [
      aws_cloudwatch_log_group.application.arn,
      "${aws_cloudwatch_log_group.application.arn}:log-stream:*"
    ]
  }

  statement {
    sid    = "TableEncryptionKeyUse"
    effect = "Allow"

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey"
    ]

    resources = [aws_kms_key.inventory.arn]
  }
}

resource "aws_iam_policy" "inventory_api" {
  name        = "${var.role_name}-policy"
  description = "Least-privilege DynamoDB, CloudWatch Logs and KMS access for ${var.project_name}"
  policy      = data.aws_iam_policy_document.inventory_api.json

  tags = {
    Name = "${var.role_name}-policy"
  }
}

resource "aws_iam_role_policy_attachment" "inventory_api" {
  role       = aws_iam_role.inventory_api.name
  policy_arn = aws_iam_policy.inventory_api.arn
}
