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

data "aws_region" "current" {}

data "aws_partition" "current" {}

locals {
  account_id     = data.aws_caller_identity.current.account_id
  region         = data.aws_region.current.name
  partition      = data.aws_partition.current.partition
  log_group_name = "/aws/lambda/${var.lambda_function_name}"
  jobs_index_arn = "${aws_dynamodb_table.jobs.arn}/index/${var.dynamodb_status_index_name}"
}

###############################################################################
# Customer managed KMS key - encrypts the jobs table, both queues, the results
# bucket, the Lambda environment and the worker log group.
###############################################################################

resource "aws_kms_key" "main" {
  description             = "CMK protecting ${var.project_name} job data at rest"
  deletion_window_in_days = var.kms_deletion_window_in_days
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableAccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
        Action    = "kms:*"
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
          "kms:DescribeKey"
        ]
        Resource = "*"
      },
      {
        Sid    = "AllowManagedServices"
        Effect = "Allow"
        Principal = {
          Service = [
            "sqs.amazonaws.com",
            "s3.amazonaws.com",
            "dynamodb.amazonaws.com",
            "lambda.amazonaws.com"
          ]
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
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
# DynamoDB - job records
###############################################################################

resource "aws_dynamodb_table" "jobs" {
  name         = var.dynamodb_table_name
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
    name            = var.dynamodb_status_index_name
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

###############################################################################
# SQS - job queue and dead-letter queue
###############################################################################

resource "aws_sqs_queue" "job_dlq" {
  name                              = var.job_dlq_name
  message_retention_seconds         = var.dlq_message_retention_seconds
  kms_master_key_id                 = aws_kms_key.main.arn
  kms_data_key_reuse_period_seconds = 300
}

resource "aws_sqs_queue" "job_queue" {
  name                              = var.job_queue_name
  visibility_timeout_seconds        = var.queue_visibility_timeout_seconds
  message_retention_seconds         = var.queue_message_retention_seconds
  receive_wait_time_seconds         = 10
  kms_master_key_id                 = aws_kms_key.main.arn
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.job_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
}

###############################################################################
# S3 - access log bucket
###############################################################################

resource "aws_s3_bucket" "logs" {
  #checkov:skip=CKV_AWS_18:This bucket is the access-log target and logs to itself via aws_s3_bucket_logging.
  #checkov:skip=CKV_AWS_21:Versioning is configured through aws_s3_bucket_versioning.
  #checkov:skip=CKV_AWS_19:Encryption is configured through aws_s3_bucket_server_side_encryption_configuration.
  #checkov:skip=CKV_AWS_145:KMS encryption is configured through aws_s3_bucket_server_side_encryption_configuration.
  #checkov:skip=CKV_AWS_144:Cross-region replication is not available on LocalStack Community and is out of scope for access logs.
  #checkov:skip=CKV2_AWS_62:Event notifications are not consumed by this application.
  bucket        = var.log_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    expiration {
      days = 365
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_s3_bucket_logging" "logs" {
  bucket        = aws_s3_bucket.logs.id
  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "self/"
}

resource "aws_s3_bucket_policy" "logs" {
  bucket = aws_s3_bucket.logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.logs.arn,
          "${aws_s3_bucket.logs.arn}/*"
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
        Resource  = "${aws_s3_bucket.logs.arn}/*"
        Condition = {
          ArnLike = {
            "aws:SourceArn" = [
              aws_s3_bucket.results.arn,
              aws_s3_bucket.logs.arn
            ]
          }
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.logs]
}

###############################################################################
# S3 - job results bucket
###############################################################################

resource "aws_s3_bucket" "results" {
  #checkov:skip=CKV_AWS_18:Access logging is configured through aws_s3_bucket_logging.
  #checkov:skip=CKV_AWS_21:Versioning is configured through aws_s3_bucket_versioning.
  #checkov:skip=CKV_AWS_19:Encryption is configured through aws_s3_bucket_server_side_encryption_configuration.
  #checkov:skip=CKV_AWS_145:KMS encryption is configured through aws_s3_bucket_server_side_encryption_configuration.
  #checkov:skip=CKV_AWS_144:Cross-region replication is unsupported on LocalStack Community; results are regenerable job output.
  #checkov:skip=CKV2_AWS_62:The application polls DynamoDB for job state; S3 event notifications are not used.
  bucket        = var.results_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_ownership_controls" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
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

resource "aws_s3_bucket_lifecycle_configuration" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    id     = "expire-job-results"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    expiration {
      days = var.results_expiration_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_s3_bucket_logging" "results" {
  bucket        = aws_s3_bucket.results.id
  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "job-results/"
}

resource "aws_s3_bucket_policy" "results" {
  bucket = aws_s3_bucket.results.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.results.arn,
          "${aws_s3_bucket.results.arn}/*"
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.results]
}

###############################################################################
# CloudWatch Logs - worker log group
###############################################################################

resource "aws_cloudwatch_log_group" "worker" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

###############################################################################
# IAM - worker execution role (least privilege)
###############################################################################

data "aws_iam_policy_document" "worker_assume_role" {
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

resource "aws_iam_role" "worker" {
  name               = "${var.lambda_function_name}-role"
  description        = "Execution role for the ${var.lambda_function_name} SQS worker"
  assume_role_policy = data.aws_iam_policy_document.worker_assume_role.json
}

data "aws_iam_policy_document" "worker" {
  statement {
    sid    = "WriteWorkerLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]
    resources = [
      aws_cloudwatch_log_group.worker.arn,
      "${aws_cloudwatch_log_group.worker.arn}:*"
    ]
  }

  statement {
    sid    = "ConsumeJobQueue"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:ChangeMessageVisibility"
    ]
    resources = [aws_sqs_queue.job_queue.arn]
  }

  statement {
    sid    = "SendToDeadLetterQueue"
    effect = "Allow"
    actions = [
      "sqs:SendMessage",
      "sqs:GetQueueAttributes"
    ]
    resources = [aws_sqs_queue.job_dlq.arn]
  }

  statement {
    sid    = "UpdateJobRecords"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:ConditionCheckItem",
      "dynamodb:DescribeTable"
    ]
    resources = [
      aws_dynamodb_table.jobs.arn,
      local.jobs_index_arn
    ]
  }

  statement {
    sid    = "StoreLargeResults"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:AbortMultipartUpload"
    ]
    resources = ["${aws_s3_bucket.results.arn}/results/*"]
  }

  statement {
    sid    = "UseDataKey"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey"
    ]
    resources = [aws_kms_key.main.arn]
  }
}

resource "aws_iam_policy" "worker" {
  name        = "${var.lambda_function_name}-policy"
  description = "Least-privilege permissions for the ${var.lambda_function_name} Lambda worker"
  policy      = data.aws_iam_policy_document.worker.json
}

resource "aws_iam_role_policy_attachment" "worker" {
  role       = aws_iam_role.worker.name
  policy_arn = aws_iam_policy.worker.arn
}

data "aws_iam_policy_document" "worker_tracing" {
  #checkov:skip=CKV_AWS_355:The X-Ray trace ingestion APIs are not resource scoped and only accept a wildcard resource.
  #checkov:skip=CKV_AWS_356:The X-Ray trace ingestion APIs are not resource scoped and only accept a wildcard resource.
  statement {
    sid    = "PublishXRayTraces"
    effect = "Allow"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords"
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "worker_tracing" {
  #checkov:skip=CKV_AWS_355:The X-Ray trace ingestion APIs are not resource scoped and only accept a wildcard resource.
  #checkov:skip=CKV_AWS_356:The X-Ray trace ingestion APIs are not resource scoped and only accept a wildcard resource.
  name        = "${var.lambda_function_name}-tracing-policy"
  description = "Allows the worker Lambda to publish X-Ray trace segments"
  policy      = data.aws_iam_policy_document.worker_tracing.json
}

resource "aws_iam_role_policy_attachment" "worker_tracing" {
  role       = aws_iam_role.worker.name
  policy_arn = aws_iam_policy.worker_tracing.arn
}

###############################################################################
# IAM - role/policy for the externally hosted FastAPI service
###############################################################################

data "aws_iam_policy_document" "api_assume_role" {
  statement {
    sid     = "AccountPrincipalsMayAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }
}

resource "aws_iam_role" "api" {
  name               = "${var.project_name}-api-service-role"
  description        = "Role assumed by the FastAPI job service"
  assume_role_policy = data.aws_iam_policy_document.api_assume_role.json
}

data "aws_iam_policy_document" "api" {
  statement {
    sid    = "ManageJobRecords"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:DescribeTable"
    ]
    resources = [
      aws_dynamodb_table.jobs.arn,
      local.jobs_index_arn
    ]
  }

  statement {
    sid    = "EnqueueJobs"
    effect = "Allow"
    actions = [
      "sqs:SendMessage",
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes"
    ]
    resources = [aws_sqs_queue.job_queue.arn]
  }

  statement {
    sid    = "InspectDeadLetterQueue"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes"
    ]
    resources = [aws_sqs_queue.job_dlq.arn]
  }

  statement {
    sid    = "ReadJobResults"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion"
    ]
    resources = ["${aws_s3_bucket.results.arn}/results/*"]
  }

  statement {
    sid    = "UseDataKey"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey"
    ]
    resources = [aws_kms_key.main.arn]
  }
}

resource "aws_iam_policy" "api" {
  name        = "${var.project_name}-api-service-policy"
  description = "Least-privilege permissions for the FastAPI job service"
  policy      = data.aws_iam_policy_document.api.json
}

resource "aws_iam_role_policy_attachment" "api" {
  role       = aws_iam_role.api.name
  policy_arn = aws_iam_policy.api.arn
}

###############################################################################
# Lambda - job worker (inline source packaged at plan time)
###############################################################################

resource "local_file" "worker_source" {
  filename        = "${path.module}/build/job_worker/index.py"
  file_permission = "0644"

  content = <<EOT
"""SQS-triggered worker for the async job processor.

Reads job messages from the job queue, executes the requested compute job,
and writes status/results back to DynamoDB (large payloads go to S3).
Failed messages are reported back to SQS so the queue redrive policy retries
them once before moving them to the dead-letter queue.
"""
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

DYNAMODB = boto3.resource('dynamodb')
S3 = boto3.client('s3')

JOBS_TABLE = os.environ['JOBS_TABLE']
RESULTS_BUCKET = os.environ['RESULTS_BUCKET']
MAX_INLINE_RESULT_BYTES = int(os.environ.get('MAX_INLINE_RESULT_BYTES', '8192'))
MAX_ATTEMPTS = int(os.environ.get('MAX_RECEIVE_COUNT', '2'))

TABLE = DYNAMODB.Table(JOBS_TABLE)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _compute(job_type, payload):
    """Execute the requested compute job."""
    if job_type == 'sum':
        return {'value': sum(Decimal(str(n)) for n in payload.get('numbers', []))}
    if job_type == 'wordcount':
        return {'value': len(str(payload.get('text', '')).split())}
    if job_type == 'sleep':
        seconds = min(float(payload.get('seconds', 1)), 5)
        time.sleep(seconds)
        return {'slept_seconds': Decimal(str(seconds))}
    if job_type == 'echo':
        return {'echo': payload}
    raise ValueError('unsupported job_type: ' + str(job_type))


def _claim(job_id, attempts):
    """Move the job to RUNNING unless it was cancelled or already finished."""
    try:
        TABLE.update_item(
            Key={'job_id': job_id},
            UpdateExpression=(
                'SET #s = :running, attempts = :a, started_at = :t, updated_at = :t'
            ),
            ConditionExpression=(
                'attribute_exists(job_id) AND #s IN (:queued, :failed, :running)'
            ),
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':running': 'RUNNING',
                ':queued': 'QUEUED',
                ':failed': 'FAILED',
                ':a': attempts,
                ':t': _now(),
            },
        )
        return True
    except ClientError as err:
        if err.response['Error']['Code'] == 'ConditionalCheckFailedException':
            print('skipping job ' + str(job_id) + ': not in a runnable state')
            return False
        raise


def _succeed(job_id, result):
    body = json.dumps(result, default=str)
    timestamp = _now()
    names = {'#s': 'status'}
    values = {':s': 'SUCCEEDED', ':t': timestamp}

    if len(body.encode('utf-8')) > MAX_INLINE_RESULT_BYTES:
        key = 'results/' + str(job_id) + '.json'
        S3.put_object(
            Bucket=RESULTS_BUCKET,
            Key=key,
            Body=body.encode('utf-8'),
            ContentType='application/json',
        )
        values[':loc'] = key
        expression = (
            'SET #s = :s, updated_at = :t, completed_at = :t, '
            'result_location = :loc REMOVE error_message'
        )
    else:
        names['#r'] = 'result'
        values[':r'] = json.loads(body, parse_float=Decimal)
        expression = (
            'SET #s = :s, updated_at = :t, completed_at = :t, '
            '#r = :r REMOVE error_message'
        )

    TABLE.update_item(
        Key={'job_id': job_id},
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _fail(job_id, attempts, message):
    status = 'DEAD_LETTER' if attempts >= MAX_ATTEMPTS else 'FAILED'
    TABLE.update_item(
        Key={'job_id': job_id},
        UpdateExpression=(
            'SET #s = :s, updated_at = :t, error_message = :e, attempts = :a'
        ),
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':s': status,
            ':t': _now(),
            ':e': str(message)[:1024],
            ':a': attempts,
        },
    )


def handler(event, context):
    failures = []

    for record in event.get('Records', []):
        message_id = record.get('messageId')
        try:
            body = json.loads(record.get('body') or '{}')
        except ValueError:
            print('dropping unparseable message ' + str(message_id))
            continue

        job_id = body.get('job_id')
        if not job_id:
            print('dropping message without job_id: ' + str(message_id))
            continue

        attributes = record.get('attributes') or {}
        attempts = int(attributes.get('ApproximateReceiveCount', '1'))

        if not _claim(job_id, attempts):
            continue

        try:
            result = _compute(body.get('job_type', ''), body.get('payload') or {})
            _succeed(job_id, result)
            print('job ' + str(job_id) + ' succeeded on attempt ' + str(attempts))
        except Exception as exc:  # noqa: BLE001 - any failure retries via SQS
            print('job ' + str(job_id) + ' failed: ' + str(exc))
            _fail(job_id, attempts, exc)
            failures.append({'itemIdentifier': message_id})

    return {'batchItemFailures': failures}
EOT
}

data "archive_file" "worker" {
  type        = "zip"
  source_file = local_file.worker_source.filename
  output_path = "${path.module}/build/job_worker.zip"

  depends_on = [local_file.worker_source]
}

resource "aws_lambda_function" "job_worker" {
  #checkov:skip=CKV_AWS_117:LocalStack Community does not provide VPC-attached Lambda networking.
  #checkov:skip=CKV_AWS_272:Code signing requires AWS Signer, which is unavailable in LocalStack Community.
  function_name                  = var.lambda_function_name
  description                    = "Consumes ${var.job_queue_name} messages and writes job results"
  role                           = aws_iam_role.worker.arn
  handler                        = "index.handler"
  runtime                        = var.lambda_runtime
  filename                       = data.archive_file.worker.output_path
  source_code_hash               = data.archive_file.worker.output_base64sha256
  timeout                        = var.lambda_timeout
  memory_size                    = var.lambda_memory_size
  reserved_concurrent_executions = var.lambda_reserved_concurrency
  kms_key_arn                    = aws_kms_key.main.arn

  dead_letter_config {
    target_arn = aws_sqs_queue.job_dlq.arn
  }

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      JOBS_TABLE              = aws_dynamodb_table.jobs.name
      JOBS_STATUS_INDEX       = var.dynamodb_status_index_name
      RESULTS_BUCKET          = aws_s3_bucket.results.bucket
      MAX_INLINE_RESULT_BYTES = tostring(var.max_inline_result_bytes)
      MAX_RECEIVE_COUNT       = tostring(var.max_receive_count)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.worker,
    aws_iam_role_policy_attachment.worker,
    aws_iam_role_policy_attachment.worker_tracing
  ]
}

resource "aws_lambda_event_source_mapping" "job_worker" {
  event_source_arn                   = aws_sqs_queue.job_queue.arn
  function_name                      = aws_lambda_function.job_worker.arn
  batch_size                         = var.lambda_batch_size
  enabled                            = true
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]
}

###############################################################################
# CloudWatch alarms - dead-letter depth and worker error rate
###############################################################################

resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  alarm_name          = "${var.project_name}-dlq-depth"
  alarm_description   = "Messages have landed on ${var.job_dlq_name} after exhausting their retry"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.job_dlq.name
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_errors" {
  alarm_name          = "${var.project_name}-worker-errors"
  alarm_description   = "The ${var.lambda_function_name} Lambda is reporting execution errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.job_worker.function_name
  }
}
