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

############################################
# Customer managed KMS key
# Encrypts the DynamoDB table, the admin API
# key secret and the CloudWatch log group.
# An explicit key policy grants account root
# administration and lets CloudWatch Logs in
# this region use the key for the app log
# group; application access is granted via
# the least-privilege IAM policy below.
############################################

resource "aws_kms_key" "this" {
  description             = "CMK for ${var.project_name} DynamoDB, Secrets Manager and CloudWatch Logs encryption"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  is_enabled              = true

  policy = jsonencode({
    Version = "2012-10-17"
    Id      = "${var.project_name}-cmk-policy"
    Statement = [
      {
        Sid    = "EnableAccountRootKeyAdministration"
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
          Service = "logs.${var.aws_region}.amazonaws.com"
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
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:*"
          }
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-cmk"
  }
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.project_name}"
  target_key_id = aws_kms_key.this.key_id
}

############################################
# DynamoDB - contact-form-messages
############################################

resource "aws_dynamodb_table" "messages" {
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
    kms_key_arn = aws_kms_key.this.arn
  }

  tags = {
    Name = var.dynamodb_table_name
  }
}

############################################
# CloudWatch Logs - /contact-form/backend
############################################

resource "aws_cloudwatch_log_group" "app" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_in_days
  kms_key_id        = aws_kms_key.this.arn

  tags = {
    Name = var.log_group_name
  }
}

############################################
# Secrets Manager - admin API key
############################################

resource "random_password" "admin_api_key" {
  length  = var.admin_api_key_length
  special = false
}

resource "aws_secretsmanager_secret" "admin_api_key" {
  #checkov:skip=CKV2_AWS_57:Automatic rotation needs a rotation Lambda, which is out of scope for this plan; the shared admin key is rotated manually.
  name                    = var.admin_api_key_secret_name
  description             = "Shared administrator API key for the contact-form backend admin endpoints"
  kms_key_id              = aws_kms_key.this.arn
  recovery_window_in_days = var.secret_recovery_window_in_days

  tags = {
    Name = var.admin_api_key_secret_name
  }
}

resource "aws_secretsmanager_secret_version" "admin_api_key" {
  secret_id     = aws_secretsmanager_secret.admin_api_key.id
  secret_string = random_password.admin_api_key.result

  lifecycle {
    ignore_changes = [secret_string]
  }
}

############################################
# IAM - application execution role
############################################

data "aws_iam_policy_document" "app_assume_role" {
  statement {
    sid     = "AllowComputeServiceToAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name                 = var.app_role_name
  description          = "Execution identity for the contact-form FastAPI service"
  assume_role_policy   = data.aws_iam_policy_document.app_assume_role.json
  max_session_duration = 3600

  tags = {
    Name = var.app_role_name
  }
}

data "aws_iam_policy_document" "app" {
  statement {
    sid    = "MessagesTableAccess"
    effect = "Allow"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:DeleteItem",
      "dynamodb:Scan",
      "dynamodb:DescribeTable",
    ]

    resources = [aws_dynamodb_table.messages.arn]
  }

  statement {
    sid    = "AdminApiKeySecretRead"
    effect = "Allow"

    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
    ]

    resources = [aws_secretsmanager_secret.admin_api_key.arn]
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

  statement {
    sid    = "EncryptionKeyUse"
    effect = "Allow"

    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]

    resources = [aws_kms_key.this.arn]
  }
}

resource "aws_iam_policy" "app" {
  name        = "${var.app_role_name}-policy"
  description = "Least-privilege access to the contact-form messages table, admin API key secret, log group and CMK"
  policy      = data.aws_iam_policy_document.app.json
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
