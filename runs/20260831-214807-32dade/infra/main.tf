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
  account_id     = data.aws_caller_identity.current.account_id
  partition      = data.aws_partition.current.partition
  region         = data.aws_region.current.name
  log_group_arn  = "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:${var.log_group_name}"
  app_role_arn   = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${var.app_role_name}"
  products_table = "arn:${data.aws_partition.current.partition}:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.products_table_name}"
}

###############################################################################
# Customer managed KMS key: encrypts the DynamoDB products table (at rest)
# and the application CloudWatch log group.
###############################################################################

data "aws_iam_policy_document" "inventory_kms_key" {
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
    sid    = "AllowDynamoDBServiceUseOfKey"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["dynamodb.amazonaws.com"]
    }

    actions = [
      "kms:CreateGrant",
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:ReEncryptFrom",
      "kms:ReEncryptTo",
    ]

    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [local.account_id]
    }
  }

  statement {
    sid    = "AllowCloudWatchLogsUseOfKey"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logs.${local.region}.amazonaws.com"]
    }

    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:ReEncryptFrom",
      "kms:ReEncryptTo",
    ]

    resources = ["*"]

    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["${local.log_group_arn}"]
    }
  }

  statement {
    sid    = "AllowApplicationRoleUseOfKey"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = [local.app_role_arn]
    }

    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:Encrypt",
      "kms:GenerateDataKey",
    ]

    resources = ["*"]
  }
}

resource "aws_kms_key" "inventory" {
  description             = "CMK for the shop inventory API DynamoDB table and application logs"
  deletion_window_in_days = var.kms_key_deletion_window_in_days
  enable_key_rotation     = true
  is_enabled              = true
  key_usage               = "ENCRYPT_DECRYPT"
  policy                  = data.aws_iam_policy_document.inventory_kms_key.json

  tags = {
    Name = "${var.project_name}-cmk"
  }
}

resource "aws_kms_alias" "inventory" {
  name          = var.kms_key_alias
  target_key_id = aws_kms_key.inventory.key_id
}

###############################################################################
# DynamoDB: single products table keyed by SKU.
# Backs POST /products, GET /products (scan), GET /products/{sku} and the
# conditional atomic UpdateItem used by PATCH /products/{sku}/stock.
###############################################################################

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

  ttl {
    attribute_name = "expires_at"
    enabled        = false
  }

  deletion_protection_enabled = false

  tags = {
    Name = var.products_table_name
  }
}

###############################################################################
# CloudWatch Logs: application log group (request + stock-adjustment audit).
###############################################################################

resource "aws_cloudwatch_log_group" "application" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_in_days
  kms_key_id        = aws_kms_key.inventory.arn

  tags = {
    Name = var.log_group_name
  }
}

###############################################################################
# IAM: execution identity for the standalone FastAPI backend, with a
# least-privilege policy covering exactly the API calls the app makes.
###############################################################################

data "aws_iam_policy_document" "app_assume_role" {
  statement {
    sid     = "AllowComputeServiceToAssumeAppRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "app" {
  name                  = var.app_role_name
  description           = "Execution identity for the shop inventory API backend service"
  assume_role_policy    = data.aws_iam_policy_document.app_assume_role.json
  force_detach_policies = true
  max_session_duration  = 3600

  tags = {
    Name = var.app_role_name
  }
}

data "aws_iam_policy_document" "app" {
  statement {
    sid    = "ProductsTableDataAccess"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Scan",
      "dynamodb:DescribeTable",
    ]

    resources = [aws_dynamodb_table.products.arn]
  }

  statement {
    sid    = "ProductsTableEncryptionKeyAccess"
    effect = "Allow"

    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:Encrypt",
      "kms:GenerateDataKey",
    ]

    resources = [aws_kms_key.inventory.arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["dynamodb.${local.region}.amazonaws.com"]
    }
  }

  statement {
    sid    = "ApplicationLogWrites"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]

    resources = [
      aws_cloudwatch_log_group.application.arn,
      "${aws_cloudwatch_log_group.application.arn}:log-stream:*",
    ]
  }
}

resource "aws_iam_policy" "app" {
  name        = var.app_policy_name
  description = "Least-privilege access to the products DynamoDB table and application log group"
  policy      = data.aws_iam_policy_document.app.json

  tags = {
    Name = var.app_policy_name
  }
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
