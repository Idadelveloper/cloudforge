###############################################################################
# Provider
###############################################################################

provider "aws" {
  region     = var.aws_region
  access_key = var.aws_access_key
  secret_key = var.aws_secret_key

  s3_use_path_style           = true
  skip_credentials_validation = true
  skip_metadata_api_check     = true

  default_tags {
    tags = {
      project      = var.project_name
      "managed-by" = "terraform"
    }
  }

  endpoints {
    cloudwatchlogs = var.aws_endpoint_url
    dynamodb       = var.aws_endpoint_url
    iam            = var.aws_endpoint_url
    kms            = var.aws_endpoint_url
    lambda         = var.aws_endpoint_url
    s3             = var.aws_endpoint_url
    secretsmanager = var.aws_endpoint_url
    sns            = var.aws_endpoint_url
    sqs            = var.aws_endpoint_url
    sts            = var.aws_endpoint_url
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_id            = data.aws_caller_identity.current.account_id
  region                = data.aws_region.current.name
  worker_log_group_name = "/aws/lambda/${var.worker_function_name}"
  jobs_status_index_arn = "${aws_dynamodb_table.jobs.arn}/index/${var.jobs_status_index_name}"
}

###############################################################################
# Customer managed key used to encrypt every data store in this stack
###############################################################################

resource "aws_kms_key" "main" {
  description             = "CMK protecting async job processor data at rest (S3, DynamoDB, SQS, SNS, Secrets Manager, CloudWatch Logs)"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  is_enabled              = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableAccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid    = "AllowServiceEncryption"
        Effect = "Allow"
        Principal = {
          Service = [
            "s3.amazonaws.com",
            "sqs.amazonaws.com",
            "sns.amazonaws.com",
            "dynamodb.amazonaws.com",
            "secretsmanager.amazonaws.com",
            "lambda.amazonaws.com",
            "logs.${data.aws_region.current.name}.amazonaws.com"
          ]
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
      }
    ]
  })
}

resource "aws_kms_alias" "main" {
  name          = "alias/${var.project_name}"
  target_key_id = aws_kms_key.main.key_id
}

###############################################################################
# S3 - access log bucket for the results bucket
###############################################################################

resource "aws_s3_bucket" "access_logs" {
  #checkov:skip=CKV_AWS_144:Cross-region replication is not available on the single-region LocalStack deployment target.
  #checkov:skip=CKV2_AWS_62:This bucket only receives S3 access logs; the application consumes no S3 event notifications.
  bucket        = var.access_logs_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "access_logs" {
  bucket                  = aws_s3_bucket.access_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
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
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_logging" "access_logs" {
  bucket        = aws_s3_bucket.access_logs.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "self-access-logs/"
}

resource "aws_s3_bucket_lifecycle_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    expiration {
      days = 90
    }
  }

  depends_on = [aws_s3_bucket_versioning.access_logs]
}

###############################################################################
# S3 - job-results-bucket (overflow store for large result payloads)
###############################################################################

resource "aws_s3_bucket" "results" {
  #checkov:skip=CKV_AWS_144:Cross-region replication is not available on the single-region LocalStack deployment target.
  #checkov:skip=CKV2_AWS_62:Results are polled through the API; the application subscribes to no S3 event notifications.
  bucket        = var.results_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "results" {
  bucket                  = aws_s3_bucket.results.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "results" {
  bucket = aws_s3_bucket.results.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_logging" "results" {
  bucket        = aws_s3_bucket.results.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "job-results-bucket/"
}

resource "aws_s3_bucket_lifecycle_configuration" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    id     = "expire-large-results"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    expiration {
      days = var.results_expiration_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.results]
}

###############################################################################
# DynamoDB - jobs and job-results
###############################################################################

resource "aws_dynamodb_table" "jobs" {
  name         = var.jobs_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = var.jobs_status_index_name
    hash_key        = "status"
    range_key       = "created_at"
    projection_type = "ALL"
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
    kms_key_arn = aws_kms_key.main.arn
  }
}

resource "aws_dynamodb_table" "job_results" {
  name         = var.results_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
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
    kms_key_arn = aws_kms_key.main.arn
  }
}

###############################################################################
# SQS - job-queue and job-dlq (retry once, then dead-letter)
###############################################################################

resource "aws_sqs_queue" "dlq" {
  name                              = var.job_dlq_name
  message_retention_seconds         = var.dlq_message_retention_seconds
  visibility_timeout_seconds        = var.queue_visibility_timeout_seconds
  receive_wait_time_seconds         = 5
  kms_master_key_id                 = aws_kms_key.main.arn
  kms_data_key_reuse_period_seconds = 300
}

resource "aws_sqs_queue" "jobs" {
  name                              = var.job_queue_name
  visibility_timeout_seconds        = var.queue_visibility_timeout_seconds
  message_retention_seconds         = var.queue_message_retention_seconds
  receive_wait_time_seconds         = 5
  kms_master_key_id                 = aws_kms_key.main.arn
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.job_max_attempts
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.jobs.arn]
  })
}

###############################################################################
# SNS - job-failure-alerts
###############################################################################

resource "aws_sns_topic" "job_failure_alerts" {
  name              = var.alerts_topic_name
  kms_master_key_id = aws_kms_key.main.arn
}

###############################################################################
# Secrets Manager - job-api-config (API submission token)
###############################################################################

resource "random_password" "api_token" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "api_config" {
  #checkov:skip=CKV2_AWS_57:Automatic rotation requires a rotation Lambda that is out of scope for this evaluation stack; the token is generated at apply time.
  name                    = var.api_secret_name
  description             = "Shared submission token and worker settings for the async job processing API"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "api_config" {
  secret_id = aws_secretsmanager_secret.api_config.id

  secret_string = jsonencode({
    api_token    = random_password.api_token.result
    max_attempts = var.job_max_attempts
  })
}

###############################################################################
# CloudWatch Logs - job-worker-log-group
###############################################################################

resource "aws_cloudwatch_log_group" "worker" {
  name              = local.worker_log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

###############################################################################
# IAM - job-worker-role (least privilege execution role)
###############################################################################

resource "aws_iam_role" "worker" {
  name        = var.worker_role_name
  description = "Execution role for the async job worker Lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "LambdaAssumeRole"
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "worker" {
  name = "${var.worker_role_name}-policy"
  role = aws_iam_role.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "WriteWorkerLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = [
          aws_cloudwatch_log_group.worker.arn,
          "${aws_cloudwatch_log_group.worker.arn}:*"
        ]
      },
      {
        Sid    = "ConsumeJobQueue"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ChangeMessageVisibility"
        ]
        Resource = [aws_sqs_queue.jobs.arn]
      },
      {
        Sid    = "UpdateJobRecords"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:ConditionCheckItem"
        ]
        Resource = [
          aws_dynamodb_table.jobs.arn,
          local.jobs_status_index_arn,
          aws_dynamodb_table.job_results.arn
        ]
      },
      {
        Sid      = "StoreLargeResults"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${aws_s3_bucket.results.arn}/*"]
      },
      {
        Sid      = "PublishFailureAlerts"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = [aws_sns_topic.job_failure_alerts.arn]
      },
      {
        Sid    = "UseDataKey"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey"
        ]
        Resource = [aws_kms_key.main.arn]
      }
    ]
  })
}

###############################################################################
# IAM - job-api-service-policy (least privilege for the FastAPI service)
###############################################################################

resource "aws_iam_policy" "api_service" {
  name        = var.api_policy_name
  description = "Least privilege access for the locally running FastAPI async job processing service"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "JobRecordAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:DescribeTable"
        ]
        Resource = [
          aws_dynamodb_table.jobs.arn,
          local.jobs_status_index_arn,
          aws_dynamodb_table.job_results.arn
        ]
      },
      {
        Sid    = "QueueAccess"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ChangeMessageVisibility"
        ]
        Resource = [
          aws_sqs_queue.jobs.arn,
          aws_sqs_queue.dlq.arn
        ]
      },
      {
        Sid    = "ResultObjectAccess"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ]
        Resource = ["${aws_s3_bucket.results.arn}/*"]
      },
      {
        Sid      = "ResultBucketListing"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.results.arn]
      },
      {
        Sid    = "ReadApiConfigSecret"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ]
        Resource = [aws_secretsmanager_secret.api_config.arn]
      },
      {
        Sid      = "PublishFailureAlerts"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = [aws_sns_topic.job_failure_alerts.arn]
      },
      {
        Sid    = "UseDataKey"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey"
        ]
        Resource = [aws_kms_key.main.arn]
      }
    ]
  })
}

###############################################################################
# Lambda - job-worker (inline source packaged at apply time)
###############################################################################

locals {
  worker_source_code = <<-PYTHON
    """SQS triggered worker for the asynchronous job processing service.

    Consumes messages from the jobs queue, executes the compute job and writes
    the terminal status to the jobs table and the payload to the results table
    (or to S3 when the serialized result is too large for DynamoDB).
    """
    import json
    import os
    import time
    from datetime import datetime, timezone
    from decimal import Decimal

    import boto3

    JOBS_TABLE = os.environ[