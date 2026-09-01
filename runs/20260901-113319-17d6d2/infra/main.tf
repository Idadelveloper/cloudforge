###############################################################################
# Loyalty points service - AWS infrastructure
#
# Every resource below maps to an entry in the shared plan's aws_resources list:
#   DynamoDB  : loyalty-customers, loyalty-transactions, loyalty-idempotency
#   SQS       : loyalty-purchases-queue, loyalty-purchases-dlq
#   SNS       : loyalty-tier-upgrades
#   S3        : loyalty-audit-log (+ its access-log bucket)
#   Lambda    : loyalty-accrual-worker
#   IAM       : loyalty-accrual-worker-role, loyalty-api-service-user
#   CloudWatch: /aws/lambda/loyalty-accrual-worker + DLQ depth alarm
#   Secrets   : loyalty-api-config
# A single customer managed KMS key encrypts all of the above at rest.
###############################################################################

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_id     = data.aws_caller_identity.current.account_id
  region         = data.aws_region.current.name
  log_group_name = "/aws/lambda/${var.accrual_worker_function_name}"
  build_dir      = "${path.module}/build/accrual_worker"
}

###############################################################################
# KMS - customer managed key used by DynamoDB, SQS, SNS, S3, Logs and Secrets
###############################################################################

resource "aws_kms_key" "loyalty" {
  description             = "Customer managed key for the loyalty points service"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableAccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = ["kms:*"]
        Resource  = "*"
      },
      {
        Sid       = "AllowCloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${local.region}.amazonaws.com" }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*"
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${local.region}:${local.account_id}:log-group:*"
          }
        }
      },
      {
        Sid    = "AllowLoyaltyServicesUseOfKey"
        Effect = "Allow"
        Principal = {
          Service = [
            "sns.amazonaws.com",
            "sqs.amazonaws.com",
            "s3.amazonaws.com",
            "lambda.amazonaws.com",
            "dynamodb.amazonaws.com",
            "secretsmanager.amazonaws.com"
          ]
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:CreateGrant",
          "kms:DescribeKey"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_kms_alias" "loyalty" {
  name          = "alias/${var.project_name}"
  target_key_id = aws_kms_key.loyalty.key_id
}

###############################################################################
# DynamoDB - customers, transactions, idempotency keys
###############################################################################

resource "aws_dynamodb_table" "customers" {
  name         = var.customers_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "customer_id"

  attribute {
    name = "customer_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.loyalty.arn
  }

  tags = {
    Name = var.customers_table_name
  }
}

resource "aws_dynamodb_table" "transactions" {
  name         = var.transactions_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "customer_id"
  range_key    = "transaction_id"

  attribute {
    name = "customer_id"
    type = "S"
  }

  attribute {
    name = "transaction_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.loyalty.arn
  }

  tags = {
    Name = var.transactions_table_name
  }
}

resource "aws_dynamodb_table" "idempotency" {
  name         = var.idempotency_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "idempotency_key"

  attribute {
    name = "idempotency_key"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.loyalty.arn
  }

  tags = {
    Name = var.idempotency_table_name
  }
}

###############################################################################
# S3 - audit log bucket plus its server access log bucket
###############################################################################

resource "aws_s3_bucket" "audit_log_access_logs" {
  #checkov:skip=CKV_AWS_144:Single-region LocalStack deployment; cross-region replication is not available.
  #checkov:skip=CKV2_AWS_62:Access-log bucket has no consumers that require event notifications.
  bucket        = var.audit_log_access_bucket_name
  force_destroy = true

  tags = {
    Name = var.audit_log_access_bucket_name
  }
}

resource "aws_s3_bucket_ownership_controls" "audit_log_access_logs" {
  bucket = aws_s3_bucket.audit_log_access_logs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "audit_log_access_logs" {
  bucket                  = aws_s3_bucket.audit_log_access_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "audit_log_access_logs" {
  bucket = aws_s3_bucket.audit_log_access_logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit_log_access_logs" {
  bucket = aws_s3_bucket.audit_log_access_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.loyalty.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "audit_log_access_logs" {
  bucket = aws_s3_bucket.audit_log_access_logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    expiration {
      days = 365
    }
  }
}

resource "aws_s3_bucket_logging" "audit_log_access_logs" {
  bucket        = aws_s3_bucket.audit_log_access_logs.id
  target_bucket = aws_s3_bucket.audit_log_access_logs.id
  target_prefix = "self-access-logs/"
}

resource "aws_s3_bucket_policy" "audit_log_access_logs" {
  bucket = aws_s3_bucket.audit_log_access_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.audit_log_access_logs.arn,
          "${aws_s3_bucket.audit_log_access_logs.arn}/*"
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
      {
        Sid       = "AllowS3ServerAccessLogDelivery"
        Effect    = "Allow"
        Principal = { Service = "logging.s3.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.audit_log_access_logs.arn}/*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.audit_log_access_logs]
}

resource "aws_s3_bucket" "audit_log" {
  #checkov:skip=CKV_AWS_144:Single-region LocalStack deployment; cross-region replication is not available.
  #checkov:skip=CKV2_AWS_62:Audit entries are read through the API; no event notification consumer exists.
  bucket        = var.audit_bucket_name
  force_destroy = true

  tags = {
    Name = var.audit_bucket_name
  }
}

resource "aws_s3_bucket_ownership_controls" "audit_log" {
  bucket = aws_s3_bucket.audit_log.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "audit_log" {
  bucket                  = aws_s3_bucket.audit_log.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "audit_log" {
  bucket = aws_s3_bucket.audit_log.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit_log" {
  bucket = aws_s3_bucket.audit_log.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.loyalty.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "audit_log" {
  bucket = aws_s3_bucket.audit_log.id

  rule {
    id     = "audit-log-retention"
    status = "Enabled"

    filter {
      prefix = "customers/"
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    noncurrent_version_expiration {
      noncurrent_days = 365
    }

    expiration {
      days = 3650
    }
  }
}

resource "aws_s3_bucket_logging" "audit_log" {
  bucket        = aws_s3_bucket.audit_log.id
  target_bucket = aws_s3_bucket.audit_log_access_logs.id
  target_prefix = "audit-log/"
}

resource "aws_s3_bucket_policy" "audit_log" {
  bucket = aws_s3_bucket.audit_log.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.audit_log.arn,
          "${aws_s3_bucket.audit_log.arn}/*"
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.audit_log]
}

###############################################################################
# SQS - purchase queue and dead-letter queue
###############################################################################

resource "aws_sqs_queue" "purchases_dlq" {
  name                              = var.purchases_dlq_name
  message_retention_seconds         = 1209600
  kms_master_key_id                 = aws_kms_key.loyalty.arn
  kms_data_key_reuse_period_seconds = 300

  tags = {
    Name = var.purchases_dlq_name
  }
}

resource "aws_sqs_queue" "purchases" {
  name                              = var.purchases_queue_name
  visibility_timeout_seconds        = var.queue_visibility_timeout_seconds
  message_retention_seconds         = 345600
  receive_wait_time_seconds         = 10
  kms_master_key_id                 = aws_kms_key.loyalty.arn
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.purchases_dlq.arn
    maxReceiveCount     = var.queue_max_receive_count
  })

  tags = {
    Name = var.purchases_queue_name
  }
}

resource "aws_sqs_queue_redrive_allow_policy" "purchases_dlq" {
  queue_url = aws_sqs_queue.purchases_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.purchases.arn]
  })
}

###############################################################################
# SNS - gold tier upgrade notifications
###############################################################################

resource "aws_sns_topic" "tier_upgrades" {
  name              = var.tier_upgrade_topic_name
  kms_master_key_id = aws_kms_key.loyalty.arn

  tags = {
    Name = var.tier_upgrade_topic_name
  }
}

###############################################################################
# Secrets Manager - API shared secret / runtime configuration
###############################################################################

resource "random_password" "api_key" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "api_config" {
  #checkov:skip=CKV2_AWS_57:Static service-to-service key rotated by operators; no rotation Lambda in LocalStack.
  name                    = var.api_config_secret_name
  description             = "Shared secret and runtime configuration for the loyalty points API"
  kms_key_id              = aws_kms_key.loyalty.arn
  recovery_window_in_days = 0

  tags = {
    Name = var.api_config_secret_name
  }
}

resource "aws_secretsmanager_secret_version" "api_config" {
  secret_id = aws_secretsmanager_secret.api_config.id

  secret_string = jsonencode({
    api_key             = random_password.api_key.result
    gold_tier_threshold = var.gold_tier_threshold
    points_per_dollar   = var.points_per_dollar
  })
}

###############################################################################
# CloudWatch - log group for the worker and an alarm on DLQ depth
###############################################################################

resource "aws_cloudwatch_log_group" "accrual_worker" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.loyalty.arn

  tags = {
    Name = local.log_group_name
  }
}

resource "aws_cloudwatch_metric_alarm" "purchases_dlq_depth" {
  alarm_name          = "${var.purchases_dlq_name}-not-empty"
  alarm_description   = "Purchase accrual messages have landed in the dead-letter queue"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.purchases_dlq.name
  }
}

###############################################################################
# IAM - accrual worker execution role
###############################################################################

data "aws_iam_policy_document" "accrual_worker_assume_role" {
  statement {
    sid     = "LambdaAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "accrual_worker" {
  name               = var.accrual_worker_role_name
  description        = "Execution role for the loyalty accrual worker Lambda"
  assume_role_policy = data.aws_iam_policy_document.accrual_worker_assume_role.json
}

data "aws_iam_policy_document" "accrual_worker" {
  statement {
    sid    = "WriteWorkerLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]
    resources = [
      aws_cloudwatch_log_group.accrual_worker.arn,
      "${aws_cloudwatch_log_group.accrual_worker.arn}:log-stream:*"
    ]
  }

  statement {
    sid    = "ConsumePurchaseQueue"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility"
    ]
    resources = [aws_sqs_queue.purchases.arn]
  }

  statement {
    sid       = "SendToDeadLetterQueue"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.purchases_dlq.arn]
  }

  statement {
    sid    = "UpdateLoyaltyTables"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:ConditionCheckItem"
    ]
    resources = [
      aws_dynamodb_table.customers.arn,
      aws_dynamodb_table.transactions.arn,
      aws_dynamodb_table.idempotency.arn
    ]
  }

  statement {
    sid       = "WriteAuditLogEntries"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.audit_log.arn}/customers/*"]
  }

  statement {
    sid       = "PublishTierUpgrades"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.tier_upgrades.arn]
  }

  statement {
    sid    = "UseLoyaltyKmsKey"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey"
    ]
    resources = [aws_kms_key.loyalty.arn]
  }
}

resource "aws_iam_role_policy" "accrual_worker" {
  name   = "${var.accrual_worker_role_name}-policy"
  role   = aws_iam_role.accrual_worker.id
  policy = data.aws_iam_policy_document.accrual_worker.json
}

###############################################################################
# IAM - least privilege identity for the externally hosted FastAPI service
###############################################################################

data "aws_iam_policy_document" "api_service_assume_role" {
  statement {
    sid     = "AccountPrincipalsMayAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
  }
}

resource "aws_iam_role" "api_service" {
  name               = var.api_service_role_name
  description        = "Least-privilege identity used by the loyalty points FastAPI service"
  assume_role_policy = data.aws_iam_policy_document.api_service_assume_role.json
}

data "aws_iam_policy_document" "api_service" {
  statement {
    sid    = "ReadWriteLoyaltyTables"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:DescribeTable"
    ]
    resources = [
      aws_dynamodb_table.customers.arn,
      aws_dynamodb_table.transactions.arn,
      aws_dynamodb_table.idempotency.arn
    ]
  }

  statement {
    sid    = "EnqueuePurchases"
    effect = "Allow"
    actions = [
      "sqs:SendMessage",
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes"
    ]
    resources = [aws_sqs_queue.purchases.arn]
  }

  statement {
    sid       = "ListAuditLogEntries"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.audit_log.arn]
  }

  statement {
    sid       = "ReadAuditLogEntries"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.audit_log.arn}/customers/*"]
  }

  statement {
    sid       = "InspectTierUpgradeTopic"
    effect    = "Allow"
    actions   = ["sns:GetTopicAttributes"]
    resources = [aws_sns_topic.tier_upgrades.arn]
  }

  statement {
    sid    = "ReadApiConfigSecret"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret"
    ]
    resources = [aws_secretsmanager_secret.api_config.arn]
  }

  statement {
    sid    = "UseLoyaltyKmsKey"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey"
    ]
    resources = [aws_kms_key.loyalty.arn]
  }
}

resource "aws_iam_role_policy" "api_service" {
  name   = "${var.api_service_role_name}-policy"
  role   = aws_iam_role.api_service.id
  policy = data.aws_iam_policy_document.api_service.json
}

###############################################################################
# Lambda - accrual worker (inline source packaged at apply time)
###############################################################################

resource "local_file" "accrual_worker_source" {
  filename             = "${local.build_dir}/handler.py"
  file_permission      = "0644"
  directory_permission = "0755"

  content = <<-PYCODE
    """Loyalty accrual worker.

    Consumes purchase messages from SQS, applies idempotent point accrual to the
    customer balance, appends an audit-log object to S3 and publishes an SNS
    notification when a customer crosses the gold-tier threshold.
    """
    import json
    import os
    from datetime import datetime, timezone
    from decimal import Decimal

    import boto3
    from botocore.exceptions import ClientError

    CUSTOMERS_TABLE = os.environ[