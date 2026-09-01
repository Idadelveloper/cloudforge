data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
}

# ===========================================================================
# DynamoDB - customer accounts, transaction ledger, idempotency keys
# ===========================================================================

resource "aws_dynamodb_table" "customers" {
  #checkov:skip=CKV_AWS_119:LocalStack Community Edition has no KMS CMK support; DynamoDB AWS-owned key encryption is enabled instead.
  name         = var.customers_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "customer_id"

  attribute {
    name = "customer_id"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }

  deletion_protection_enabled = false

  tags = {
    Name      = var.customers_table_name
    component = "customer-accounts"
  }
}

resource "aws_dynamodb_table" "transactions" {
  #checkov:skip=CKV_AWS_119:LocalStack Community Edition has no KMS CMK support; DynamoDB AWS-owned key encryption is enabled instead.
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

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }

  deletion_protection_enabled = false

  tags = {
    Name      = var.transactions_table_name
    component = "transaction-ledger"
  }
}

resource "aws_dynamodb_table" "idempotency" {
  #checkov:skip=CKV_AWS_119:LocalStack Community Edition has no KMS CMK support; DynamoDB AWS-owned key encryption is enabled instead.
  name         = var.idempotency_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "idempotency_key"

  attribute {
    name = "idempotency_key"
    type = "S"
  }

  ttl {
    attribute_name = var.idempotency_ttl_attribute
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }

  deletion_protection_enabled = false

  tags = {
    Name      = var.idempotency_table_name
    component = "idempotency"
  }
}

# ===========================================================================
# SQS - purchase accrual queue plus dead-letter queue
# ===========================================================================

resource "aws_sqs_queue" "purchases_dlq" {
  #checkov:skip=CKV2_AWS_73:LocalStack Community Edition has no KMS CMK support; SQS-managed SSE (SSE-SQS) is enabled instead.
  name                      = var.purchases_dlq_name
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true

  tags = {
    Name      = var.purchases_dlq_name
    component = "purchase-dlq"
  }
}

resource "aws_sqs_queue" "purchases" {
  #checkov:skip=CKV2_AWS_73:LocalStack Community Edition has no KMS CMK support; SQS-managed SSE (SSE-SQS) is enabled instead.
  name                       = var.purchases_queue_name
  visibility_timeout_seconds = var.purchases_queue_visibility_timeout
  message_retention_seconds  = 345600
  receive_wait_time_seconds  = 5
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.purchases_dlq.arn
    maxReceiveCount     = var.purchases_queue_max_receive_count
  })

  tags = {
    Name      = var.purchases_queue_name
    component = "purchase-queue"
  }
}

resource "aws_sqs_queue_redrive_allow_policy" "purchases_dlq" {
  queue_url = aws_sqs_queue.purchases_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.purchases.arn]
  })
}

# ===========================================================================
# SNS - gold tier upgrade announcements
# ===========================================================================

resource "aws_sns_topic" "gold_upgrades" {
  name              = var.gold_upgrades_topic_name
  kms_master_key_id = "alias/aws/sns"

  tags = {
    Name      = var.gold_upgrades_topic_name
    component = "tier-upgrade-notifications"
  }
}

# ===========================================================================
# S3 - audit log bucket (+ access log bucket)
# ===========================================================================

resource "aws_s3_bucket" "access_logs" {
  #checkov:skip=CKV_AWS_18:This bucket is itself the access-log target; logging into itself would recurse.
  #checkov:skip=CKV_AWS_144:LocalStack Community Edition is single-region; cross-region replication is not available.
  #checkov:skip=CKV_AWS_145:LocalStack Community Edition has no KMS CMK support; SSE-S3 (AES256) is used.
  #checkov:skip=CKV2_AWS_62:Access-log delivery does not require S3 event notifications for this service.
  bucket        = var.access_logs_bucket_name
  force_destroy = true

  tags = {
    Name      = var.access_logs_bucket_name
    component = "audit-access-logs"
  }
}

resource "aws_s3_bucket_versioning" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "access_logs" {
  bucket                  = aws_s3_bucket.access_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = 90
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket" "audit" {
  #checkov:skip=CKV_AWS_144:LocalStack Community Edition is single-region; cross-region replication is not available.
  #checkov:skip=CKV_AWS_145:LocalStack Community Edition has no KMS CMK support; SSE-S3 (AES256) is used.
  #checkov:skip=CKV2_AWS_62:The audit log is written and read directly by the service; no S3 event notification consumer exists.
  bucket        = var.audit_bucket_name
  force_destroy = true

  tags = {
    Name      = var.audit_bucket_name
    component = "balance-change-audit-log"
  }
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_logging" "audit" {
  bucket        = aws_s3_bucket.audit.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "s3-access-logs/${var.audit_bucket_name}/"
}

resource "aws_s3_bucket_lifecycle_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    id     = "audit-retention"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.audit_noncurrent_version_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# ===========================================================================
# CloudWatch - service log group and DLQ backlog alarm
# ===========================================================================

resource "aws_cloudwatch_log_group" "service" {
  #checkov:skip=CKV_AWS_158:LocalStack Community Edition has no KMS CMK support; log data uses the default CloudWatch Logs encryption.
  name              = var.log_group_name
  retention_in_days = var.log_retention_days

  tags = {
    Name      = var.log_group_name
    component = "service-logs"
  }
}

resource "aws_cloudwatch_metric_alarm" "purchases_dlq_backlog" {
  alarm_name          = "${var.purchases_dlq_name}-messages-visible"
  alarm_description   = "Purchase messages have landed in the loyalty purchases dead-letter queue."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.dlq_alarm_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.purchases_dlq.name
  }

  tags = {
    Name      = "${var.purchases_dlq_name}-messages-visible"
    component = "observability"
  }
}

# ===========================================================================
# Secrets Manager - runtime service configuration (shared API key)
# ===========================================================================

resource "random_password" "service_api_key" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "service_config" {
  #checkov:skip=CKV_AWS_149:LocalStack Community Edition has no KMS CMK support; the AWS-managed Secrets Manager key is used.
  #checkov:skip=CKV2_AWS_57:Automatic rotation requires a Lambda rotation function which is out of scope for this service.
  name                    = var.service_config_secret_name
  description             = "Runtime configuration for the loyalty points service (shared API key)."
  recovery_window_in_days = var.secret_recovery_window_days

  tags = {
    Name      = var.service_config_secret_name
    component = "service-config"
  }
}

resource "aws_secretsmanager_secret_version" "service_config" {
  secret_id = aws_secretsmanager_secret.service_config.id

  secret_string = jsonencode({
    api_key             = random_password.service_api_key.result
    gold_tier_threshold = var.gold_tier_threshold
    points_per_currency = 1
  })
}

# ===========================================================================
# IAM - least privilege role for the backend service
# ===========================================================================

data "aws_iam_policy_document" "service_assume_role" {
  statement {
    sid     = "AllowComputeServicesToAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com", "ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "service" {
  name               = var.service_role_name
  description        = "Least-privilege role for the loyalty points service API and SQS worker."
  assume_role_policy = data.aws_iam_policy_document.service_assume_role.json

  tags = {
    Name      = var.service_role_name
    component = "service-identity"
  }
}

data "aws_iam_policy_document" "service" {
  statement {
    sid    = "LoyaltyTablesReadWrite"
    effect = "Allow"

    actions = [
      "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:ConditionCheckItem",
      "dynamodb:DeleteItem",
      "dynamodb:DescribeTable",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
      "dynamodb:UpdateItem",
    ]

    resources = [
      aws_dynamodb_table.customers.arn,
      aws_dynamodb_table.transactions.arn,
      aws_dynamodb_table.idempotency.arn,
      "${aws_dynamodb_table.customers.arn}/index/*",
      "${aws_dynamodb_table.transactions.arn}/index/*",
      "${aws_dynamodb_table.idempotency.arn}/index/*",
    ]
  }

  statement {
    sid    = "PurchaseQueueAccess"
    effect = "Allow"

    actions = [
      "sqs:ChangeMessageVisibility",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:ReceiveMessage",
      "sqs:SendMessage",
    ]

    resources = [
      aws_sqs_queue.purchases.arn,
      aws_sqs_queue.purchases_dlq.arn,
    ]
  }

  statement {
    sid    = "GoldUpgradePublish"
    effect = "Allow"

    actions = [
      "sns:Publish",
      "sns:GetTopicAttributes",
    ]

    resources = [aws_sns_topic.gold_upgrades.arn]
  }

  statement {
    sid    = "AuditLogObjectWrite"
    effect = "Allow"

    actions = [
      "s3:PutObject",
      "s3:GetObject",
    ]

    resources = ["${aws_s3_bucket.audit.arn}/${var.audit_log_prefix}*"]
  }

  statement {
    sid    = "AuditLogBucketList"
    effect = "Allow"

    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]

    resources = [aws_s3_bucket.audit.arn]
  }

  statement {
    sid    = "ServiceConfigSecretRead"
    effect = "Allow"

    actions = [
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetSecretValue",
    ]

    resources = [aws_secretsmanager_secret.service_config.arn]
  }

  statement {
    sid    = "ServiceLogWrite"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
    ]

    resources = [
      aws_cloudwatch_log_group.service.arn,
      "${aws_cloudwatch_log_group.service.arn}:log-stream:*",
    ]
  }
}

resource "aws_iam_policy" "service" {
  name        = "${var.service_role_name}-policy"
  description = "Least-privilege permissions for the loyalty points service."
  policy      = data.aws_iam_policy_document.service.json

  tags = {
    Name      = "${var.service_role_name}-policy"
    component = "service-identity"
  }
}

resource "aws_iam_role_policy_attachment" "service" {
  role       = aws_iam_role.service.name
  policy_arn = aws_iam_policy.service.arn
}
