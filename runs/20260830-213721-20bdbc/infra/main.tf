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

data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# Customer managed KMS key
# Used to encrypt the notes DynamoDB table and the application log group.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "notes" {
  description             = "CMK for the ${var.project_name} notes table and application logs"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowAccountAdministration"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
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
          "kms:ScheduleKeyDeletion",
          "kms:CancelKeyDeletion",
          "kms:TagResource",
          "kms:UntagResource"
        ]
        Resource = "*"
      },
      {
        Sid    = "AllowCloudWatchLogsUse"
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
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-cmk"
  }
}

resource "aws_kms_alias" "notes" {
  name          = "alias/${var.project_name}"
  target_key_id = aws_kms_key.notes.key_id
}

# ---------------------------------------------------------------------------
# DynamoDB: notes table (the only datastore the application uses)
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "notes" {
  name         = var.notes_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = var.notes_table_hash_key

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

  ttl {
    attribute_name = "expires_at"
    enabled        = false
  }

  tags = {
    Name = var.notes_table_name
  }
}

# ---------------------------------------------------------------------------
# CloudWatch Logs: application log group
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "application" {
  name              = var.application_log_group_name
  retention_in_days = var.log_retention_in_days
  kms_key_id        = aws_kms_key.notes.arn

  tags = {
    Name = var.application_log_group_name
  }
}

# ---------------------------------------------------------------------------
# IAM: least-privilege service role for the notes API
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "service_assume_role" {
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

resource "aws_iam_role" "notes_api_service" {
  name               = var.service_role_name
  description        = "Role used by the ${var.project_name} FastAPI service to access DynamoDB and CloudWatch Logs"
  assume_role_policy = data.aws_iam_policy_document.service_assume_role.json

  tags = {
    Name = var.service_role_name
  }
}

data "aws_iam_policy_document" "notes_api_service" {
  statement {
    sid    = "NotesTableCrud"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:DescribeTable"
    ]

    resources = [
      aws_dynamodb_table.notes.arn
    ]
  }

  statement {
    sid    = "ApplicationLogWrites"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams"
    ]

    resources = [
      "${aws_cloudwatch_log_group.application.arn}:*"
    ]
  }

  statement {
    sid    = "UseCmkForTableAndLogs"
    effect = "Allow"

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncryptFrom",
      "kms:ReEncryptTo",
      "kms:GenerateDataKey",
      "kms:DescribeKey"
    ]

    resources = [
      aws_kms_key.notes.arn
    ]
  }
}

resource "aws_iam_policy" "notes_api_service" {
  name        = var.service_policy_name
  description = "Least-privilege access to the ${var.notes_table_name} table, its CMK and the application log group"
  policy      = data.aws_iam_policy_document.notes_api_service.json

  tags = {
    Name = var.service_policy_name
  }
}

resource "aws_iam_role_policy_attachment" "notes_api_service" {
  role       = aws_iam_role.notes_api_service.name
  policy_arn = aws_iam_policy.notes_api_service.arn
}
