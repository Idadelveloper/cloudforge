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

#########################################
# Customer managed KMS key
# Used to encrypt the DynamoDB table and
# the CloudWatch log group at rest. An
# explicit key policy is attached: the
# account root administers the key, the
# CloudWatch Logs service may use it for
# the application log group, and the app
# role may use it for data key operations.
#########################################

resource "aws_kms_key" "contact_form" {
  description             = "CMK for the contact form backend (DynamoDB table and CloudWatch logs)"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  is_enabled              = true

  policy = jsonencode({
    Version = "2012-10-17"
    Id      = "contact-form-backend-cmk-policy"
    Statement = [
      {
        Sid    = "EnableAccountKeyAdministration"
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
          "kms:TagResource",
          "kms:UntagResource",
          "kms:ScheduleKeyDeletion",
          "kms:CancelKeyDeletion"
        ]
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
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:${var.log_group_name}"
          }
        }
      },
      {
        Sid    = "AllowApplicationRoleDataKeyUse"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.app_role_name}"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey",
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

resource "aws_kms_alias" "contact_form" {
  name          = "alias/contact-form-backend"
  target_key_id = aws_kms_key.contact_form.key_id
}

#########################################
# DynamoDB: contact-messages
# Partition key: id (string, UUID4)
#########################################

resource "aws_dynamodb_table" "contact_messages" {
  name         = var.messages_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.contact_form.arn
  }

  point_in_time_recovery {
    enabled = true
  }

  deletion_protection_enabled = false

  tags = {
    Name = var.messages_table_name
  }
}

#########################################
# CloudWatch Logs: application log group
#########################################

resource "aws_cloudwatch_log_group" "app" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.contact_form.arn

  tags = {
    Name = var.log_group_name
  }
}

#########################################
# IAM: application execution role with a
# least-privilege policy on the table and
# the application log group only.
#########################################

data "aws_iam_policy_document" "app_assume_role" {
  statement {
    sid     = "AllowServiceAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }

  statement {
    sid     = "AllowAccountPrincipalsAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

resource "aws_iam_role" "app" {
  name                 = var.app_role_name
  description          = "Execution identity for the contact form backend service"
  assume_role_policy   = data.aws_iam_policy_document.app_assume_role.json
  max_session_duration = 3600

  tags = {
    Name = var.app_role_name
  }
}

data "aws_iam_policy_document" "app_permissions" {
  statement {
    sid    = "ContactMessagesTableAccess"
    effect = "Allow"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:Scan",
      "dynamodb:DeleteItem",
      "dynamodb:DescribeTable",
    ]

    resources = [aws_dynamodb_table.contact_messages.arn]
  }

  statement {
    sid    = "ContactFormLogWrite"
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
    sid    = "UseCmkForTableAndLogs"
    effect = "Allow"

    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]

    resources = [aws_kms_key.contact_form.arn]
  }
}

resource "aws_iam_policy" "app" {
  name        = var.app_policy_name
  description = "Least-privilege access to the contact-messages table and application log group"
  policy      = data.aws_iam_policy_document.app_permissions.json

  tags = {
    Name = var.app_policy_name
  }
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
