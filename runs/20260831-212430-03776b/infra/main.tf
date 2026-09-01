provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project      = var.project_name
      "managed-by" = var.managed_by
      environment  = var.environment
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

# ---------------------------------------------------------------------------
# KMS customer managed key
# Used to encrypt the DynamoDB messages table, the admin API key secret and
# the application CloudWatch log group.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "contact_form" {
  description             = "CMK for the ${var.project_name} DynamoDB table, secret and log group"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  is_enabled              = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableAccountRootKeyAdministration"
        Effect = "Allow"
        Principal = {
          AWS = "arn:${local.partition}:iam::${local.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowCloudWatchLogsUseOfTheKey"
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
            "kms:EncryptionContext:aws:logs:arn" = "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:${var.log_group_name}"
          }
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-cmk"
  }
}

resource "aws_kms_alias" "contact_form" {
  name          = "alias/${var.project_name}"
  target_key_id = aws_kms_key.contact_form.key_id
}

# ---------------------------------------------------------------------------
# DynamoDB: contact-form-messages
# Primary datastore for submitted messages (PutItem / GetItem / Scan / DeleteItem)
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "messages" {
  name         = var.messages_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = var.messages_table_hash_key

  attribute {
    name = var.messages_table_hash_key
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.contact_form.arn
  }

  tags = {
    Name = var.messages_table_name
  }
}

# ---------------------------------------------------------------------------
# Secrets Manager: contact-form/admin-api-key
# Credential used by the app to validate the X-Admin-API-Key header.
# ---------------------------------------------------------------------------
resource "random_password" "admin_api_key" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "admin_api_key" {
  # checkov:skip=CKV2_AWS_57:Automatic rotation requires a rotation Lambda which is out of scope for this LocalStack deployment; the key is rotated manually.
  name                    = var.admin_api_key_secret_name
  description             = "Admin API key for the ${var.project_name} admin endpoints"
  kms_key_id              = aws_kms_key.contact_form.arn
  recovery_window_in_days = var.secret_recovery_window_in_days

  tags = {
    Name = var.admin_api_key_secret_name
  }
}

resource "aws_secretsmanager_secret_version" "admin_api_key" {
  secret_id     = aws_secretsmanager_secret.admin_api_key.id
  secret_string = random_password.admin_api_key.result
}

# ---------------------------------------------------------------------------
# CloudWatch Logs: /contact-form/backend
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "backend" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_in_days
  kms_key_id        = aws_kms_key.contact_form.arn

  tags = {
    Name = var.log_group_name
  }
}

# ---------------------------------------------------------------------------
# IAM: contact-form-backend-app-role (least privilege)
# ---------------------------------------------------------------------------
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
  name               = var.app_role_name
  description        = "Least-privilege role for the ${var.project_name} service"
  assume_role_policy = data.aws_iam_policy_document.app_assume_role.json

  tags = {
    Name = var.app_role_name
  }
}

data "aws_iam_policy_document" "app_permissions" {
  statement {
    sid    = "MessagesTableItemAccess"
    effect = "Allow"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:DescribeTable"
    ]

    resources = [aws_dynamodb_table.messages.arn]
  }

  statement {
    sid    = "ReadAdminApiKeySecret"
    effect = "Allow"

    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret"
    ]

    resources = [aws_secretsmanager_secret.admin_api_key.arn]
  }

  statement {
    sid    = "WriteApplicationLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams"
    ]

    resources = [
      aws_cloudwatch_log_group.backend.arn,
      "${aws_cloudwatch_log_group.backend.arn}:log-stream:*"
    ]
  }

  statement {
    sid    = "UseCmkForTableAndSecret"
    effect = "Allow"

    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey"
    ]

    resources = [aws_kms_key.contact_form.arn]
  }
}

resource "aws_iam_policy" "app" {
  name        = "${var.app_role_name}-policy"
  description = "Least-privilege permissions for the ${var.project_name} service"
  policy      = data.aws_iam_policy_document.app_permissions.json

  tags = {
    Name = "${var.app_role_name}-policy"
  }
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
