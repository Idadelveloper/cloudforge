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

########################################
# Encryption key (used by DynamoDB table + CloudWatch log group)
########################################

data "aws_iam_policy_document" "notes_kms" {
  statement {
    sid    = "EnableAccountKeyAdministration"
    effect = "Allow"

    actions = [
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
    ]

    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  statement {
    sid    = "AllowDynamoDbUseOfTheKey"
    effect = "Allow"

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncryptFrom",
      "kms:ReEncryptTo",
      "kms:GenerateDataKey",
      "kms:GenerateDataKeyWithoutPlaintext",
      "kms:DescribeKey",
      "kms:CreateGrant",
    ]

    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["dynamodb.amazonaws.com"]
    }
  }

  statement {
    sid    = "AllowCloudWatchLogsUseOfTheKey"
    effect = "Allow"

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]

    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["logs.${data.aws_region.current.name}.amazonaws.com"]
    }
  }
}

resource "aws_kms_key" "notes" {
  description             = "CMK for the ${var.project_name} DynamoDB table and application log group"
  deletion_window_in_days = var.kms_deletion_window_in_days
  enable_key_rotation     = true
  is_enabled              = true
  policy                  = data.aws_iam_policy_document.notes_kms.json

  tags = {
    Name = "${var.project_name}-cmk"
  }
}

resource "aws_kms_alias" "notes" {
  name          = var.kms_key_alias
  target_key_id = aws_kms_key.notes.key_id
}

########################################
# DynamoDB: notes table (plan aws_resources -> DynamoDB "notes")
########################################

resource "aws_dynamodb_table" "notes" {
  name         = var.notes_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
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

########################################
# CloudWatch Logs: application log group (plan aws_resources -> CloudWatch)
########################################

resource "aws_cloudwatch_log_group" "app" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.notes.arn

  tags = {
    Name = var.log_group_name
  }
}

########################################
# IAM: least-privilege policy + role for the API service
# (plan aws_resources -> IAM "notes-api-app-policy")
########################################

data "aws_iam_policy_document" "app_assume_role" {
  statement {
    sid     = "AllowComputeToAssumeNotesApiRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "app" {
  statement {
    sid    = "NotesTableCrudAccess"
    effect = "Allow"

    actions = [
      "dynamodb:DescribeTable",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
    ]

    resources = [aws_dynamodb_table.notes.arn]
  }

  statement {
    sid    = "NotesApplicationLogWrites"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
    ]

    resources = [
      aws_cloudwatch_log_group.app.arn,
      "${aws_cloudwatch_log_group.app.arn}:log-stream:*",
    ]
  }

  statement {
    sid    = "NotesEncryptionKeyUsage"
    effect = "Allow"

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncryptFrom",
      "kms:ReEncryptTo",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]

    resources = [aws_kms_key.notes.arn]
  }
}

resource "aws_iam_policy" "app" {
  name        = var.app_policy_name
  description = "Least-privilege access for the ${var.project_name} service: CRUD on the notes table plus application log writes"
  policy      = data.aws_iam_policy_document.app.json
}

resource "aws_iam_role" "app" {
  name               = var.app_role_name
  description        = "Role assumed by the ${var.project_name} FastAPI service"
  assume_role_policy = data.aws_iam_policy_document.app_assume_role.json

  tags = {
    Name = var.app_role_name
  }
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
