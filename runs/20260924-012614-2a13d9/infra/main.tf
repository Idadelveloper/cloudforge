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

data "aws_partition" "current" {}

data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  region     = data.aws_region.current.name
}

#############################################
# Customer managed KMS key
# Encrypts the notes DynamoDB table and the
# application CloudWatch log group at rest.
#############################################
resource "aws_kms_key" "notes" {
  description             = "CMK encrypting the ${var.notes_table_name} DynamoDB table and the notes API log group"
  deletion_window_in_days = var.kms_key_deletion_window_days
  enable_key_rotation     = true
  is_enabled              = true

  policy = jsonencode({
    Version = "2012-10-17"
    Id      = "personal-notes-api-key-policy"
    Statement = [
      {
        Sid    = "AllowAccountAdministration"
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
        Resource = "*"
      },
      {
        Sid    = "AllowDynamoDbServiceUse"
        Effect = "Allow"
        Principal = {
          Service = "dynamodb.amazonaws.com"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
          "kms:CreateGrant"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:CallerAccount" = local.account_id
          }
        }
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
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = "*"
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

resource "aws_kms_alias" "notes" {
  name          = "alias/personal-notes-api"
  target_key_id = aws_kms_key.notes.key_id
}

#############################################
# DynamoDB: notes table
# Backs POST/GET/PUT/DELETE /notes endpoints.
#############################################
resource "aws_dynamodb_table" "notes" {
  name                        = var.notes_table_name
  billing_mode                = var.dynamodb_billing_mode
  hash_key                    = var.notes_table_hash_key
  deletion_protection_enabled = var.enable_deletion_protection

  attribute {
    name = var.notes_table_hash_key
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.notes.arn
  }

  tags = {
    Name = var.notes_table_name
  }
}

#############################################
# CloudWatch Logs: application log group
#############################################
resource "aws_cloudwatch_log_group" "notes_api" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.notes.arn

  tags = {
    Name = var.log_group_name
  }
}

#############################################
# IAM: least-privilege application role
#############################################
resource "aws_iam_role" "app" {
  name                 = var.app_role_name
  description          = "Least-privilege role used by the personal notes API to access the notes table and its log group"
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowServiceAssumeRole"
        Effect = "Allow"
        Principal = {
          Service = var.app_role_trusted_services
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
      }
    ]
  })

  tags = {
    Name = var.app_role_name
  }
}

resource "aws_iam_policy" "app" {
  name        = "${var.app_role_name}-policy"
  description = "Grants CRUD access to the notes DynamoDB table, log writes and use of the notes CMK"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "NotesTableCrud"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Scan",
          "dynamodb:DescribeTable"
        ]
        Resource = [
          aws_dynamodb_table.notes.arn
        ]
      },
      {
        Sid    = "NotesApiLogWrite"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = [
          aws_cloudwatch_log_group.notes_api.arn,
          "${aws_cloudwatch_log_group.notes_api.arn}:log-stream:*"
        ]
      },
      {
        Sid    = "NotesCmkUse"
        Effect = "Allow"
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = [
          aws_kms_key.notes.arn
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
