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

  log_group_arn_pattern = "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:${var.log_group_name}"
}

# ---------------------------------------------------------------------------
# Customer managed KMS key: encrypts DynamoDB items at rest and CloudWatch log
# data for the shortener service.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "kms_key" {
  statement {
    sid    = "EnableAccountKeyAdministration"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }

    actions   = ["kms:*"]
    resources = ["*"]
  }

  statement {
    sid    = "AllowCloudWatchLogsUseOfTheKey"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logs.${local.region}.amazonaws.com"]
    }

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]

    resources = ["*"]

    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["${local.log_group_arn_pattern}"]
    }
  }

  statement {
    sid    = "AllowDynamoDbUseOfTheKey"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["dynamodb.amazonaws.com"]
    }

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]

    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_kms_key" "url_shortener" {
  description             = "Customer managed key for the ${var.project_name} DynamoDB table and log group"
  deletion_window_in_days = var.kms_key_deletion_window_in_days
  enable_key_rotation     = true
  is_enabled              = true
  policy                  = data.aws_iam_policy_document.kms_key.json

  tags = {
    Name = "${var.project_name}-key"
  }
}

resource "aws_kms_alias" "url_shortener" {
  name          = var.kms_key_alias
  target_key_id = aws_kms_key.url_shortener.key_id
}

# ---------------------------------------------------------------------------
# DynamoDB: primary storage for short code -> long URL mappings and the
# atomically incremented visit counter.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "url_shortener_mappings" {
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
    kms_key_arn = aws_kms_key.url_shortener.arn
  }

  tags = {
    Name = var.dynamodb_table_name
  }
}

# ---------------------------------------------------------------------------
# CloudWatch Logs: structured application logs (link creation, redirects, 404s).
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "url_shortener" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_in_days
  kms_key_id        = aws_kms_key.url_shortener.arn

  tags = {
    Name = var.log_group_name
  }
}

# ---------------------------------------------------------------------------
# IAM: execution identity for the backend service, scoped to the mappings
# table and its own log group only.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "service_assume_role" {
  statement {
    sid     = "AllowServiceRuntimeToAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "url_shortener_service_role" {
  name                 = var.service_role_name
  description          = "Execution identity for the ${var.project_name} service"
  assume_role_policy   = data.aws_iam_policy_document.service_assume_role.json
  max_session_duration = 3600

  tags = {
    Name = var.service_role_name
  }
}

data "aws_iam_policy_document" "url_shortener_dynamodb" {
  statement {
    sid    = "MappingsTableItemAccess"
    effect = "Allow"

    actions = [
      "dynamodb:DescribeTable",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:BatchGetItem",
    ]

    resources = [
      aws_dynamodb_table.url_shortener_mappings.arn,
    ]
  }

  statement {
    sid    = "MappingsTableKmsAccess"
    effect = "Allow"

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]

    resources = [
      aws_kms_key.url_shortener.arn,
    ]
  }

  statement {
    sid    = "ApplicationLogWriteAccess"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]

    resources = [
      aws_cloudwatch_log_group.url_shortener.arn,
      "${aws_cloudwatch_log_group.url_shortener.arn}:*",
      "${local.log_group_arn_pattern}:log-stream:*",
    ]
  }
}

resource "aws_iam_policy" "url_shortener_dynamodb_policy" {
  name        = var.dynamodb_policy_name
  description = "Least-privilege access to the ${var.dynamodb_table_name} table and the service log group"
  policy      = data.aws_iam_policy_document.url_shortener_dynamodb.json

  tags = {
    Name = var.dynamodb_policy_name
  }
}

resource "aws_iam_role_policy_attachment" "url_shortener_dynamodb_policy" {
  role       = aws_iam_role.url_shortener_service_role.name
  policy_arn = aws_iam_policy.url_shortener_dynamodb_policy.arn
}
