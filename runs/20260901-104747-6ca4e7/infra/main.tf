provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project      = var.project
      "managed-by" = var.managed_by
    }
  }
}

data "aws_caller_identity" "current" {}

#############################################
# DynamoDB: blog-posts
#############################################

resource "aws_dynamodb_table" "posts" {
  # checkov:skip=CKV_AWS_119: LocalStack Community Edition target keeps AWS-owned DynamoDB encryption; no customer managed KMS key is provisioned.
  # checkov:skip=CKV_AWS_28: point-in-time recovery is enabled below via the point_in_time_recovery block.
  name         = var.posts_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "post_id"

  attribute {
    name = "post_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  deletion_protection_enabled = false

  tags = {
    Name = var.posts_table_name
    role = "posts-store"
  }
}

#############################################
# DynamoDB: blog-published-comments
#############################################

resource "aws_dynamodb_table" "published_comments" {
  # checkov:skip=CKV_AWS_119: LocalStack Community Edition target keeps AWS-owned DynamoDB encryption; no customer managed KMS key is provisioned.
  # checkov:skip=CKV_AWS_28: point-in-time recovery is enabled below via the point_in_time_recovery block.
  name         = var.published_comments_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "post_id"
  range_key    = "comment_id"

  attribute {
    name = "post_id"
    type = "S"
  }

  attribute {
    name = "comment_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  deletion_protection_enabled = false

  tags = {
    Name = var.published_comments_table_name
    role = "published-comments-store"
  }
}

#############################################
# S3: access log bucket for the images bucket
#############################################

resource "aws_s3_bucket" "logs" {
  # checkov:skip=CKV_AWS_18: this bucket is itself the access-log destination; self-logging would recurse.
  # checkov:skip=CKV_AWS_144: cross-region replication is out of scope for the LocalStack deployment target.
  # checkov:skip=CKV_AWS_145: SSE-S3 (AES256) is used because no customer managed KMS key is provisioned for LocalStack Community.
  # checkov:skip=CKV2_AWS_62: event notifications are not required for an access-log bucket.
  # checkov:skip=CKV2_AWS_6: a public access block is declared for this bucket below.
  # checkov:skip=CKV2_AWS_61: a lifecycle configuration is declared for this bucket below.
  bucket        = var.logs_bucket_name
  force_destroy = true

  tags = {
    Name = var.logs_bucket_name
    role = "access-logs"
  }
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket = aws_s3_bucket.logs.id

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
      sse_algorithm = "AES256"
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

resource "aws_s3_bucket_ownership_controls" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_policy" "logs" {
  bucket = aws_s3_bucket.logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowS3ServerAccessLogsDelivery"
        Effect = "Allow"
        Principal = {
          Service = "logging.s3.amazonaws.com"
        }
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.logs.arn}/images-access-logs/*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
          ArnLike = {
            "aws:SourceArn" = aws_s3_bucket.images.arn
          }
        }
      }
    ]
  })
}

#############################################
# S3: blog-post-images
#############################################

resource "aws_s3_bucket" "images" {
  # checkov:skip=CKV_AWS_144: cross-region replication is out of scope for the LocalStack deployment target.
  # checkov:skip=CKV_AWS_145: SSE-S3 (AES256) is used because no customer managed KMS key is provisioned for LocalStack Community.
  # checkov:skip=CKV2_AWS_62: the application reads images synchronously via presigned URLs; no event notifications are required.
  # checkov:skip=CKV2_AWS_6: a public access block is declared for this bucket below.
  # checkov:skip=CKV2_AWS_61: a lifecycle configuration is declared for this bucket below.
  bucket        = var.images_bucket_name
  force_destroy = true

  tags = {
    Name = var.images_bucket_name
    role = "post-images"
  }
}

resource "aws_s3_bucket_public_access_block" "images" {
  bucket = aws_s3_bucket.images.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "images" {
  bucket = aws_s3_bucket.images.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "images" {
  bucket = aws_s3_bucket.images.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_ownership_controls" "images" {
  bucket = aws_s3_bucket.images.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_logging" "images" {
  bucket = aws_s3_bucket.images.id

  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "images-access-logs/"
}

resource "aws_s3_bucket_lifecycle_configuration" "images" {
  bucket = aws_s3_bucket.images.id

  rule {
    id     = "expire-noncurrent-image-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.images_noncurrent_version_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

#############################################
# SQS: comment moderation queue + DLQ
#############################################

resource "aws_sqs_queue" "moderation_dlq" {
  # checkov:skip=CKV_AWS_27: SQS-managed server-side encryption (SSE-SQS) is enabled; no customer managed KMS key exists on LocalStack Community.
  name                       = var.moderation_dlq_name
  message_retention_seconds  = var.moderation_dlq_retention_seconds
  visibility_timeout_seconds = var.moderation_queue_visibility_timeout
  sqs_managed_sse_enabled    = true

  tags = {
    Name = var.moderation_dlq_name
    role = "comment-moderation-dlq"
  }
}

resource "aws_sqs_queue" "moderation" {
  # checkov:skip=CKV_AWS_27: SQS-managed server-side encryption (SSE-SQS) is enabled; no customer managed KMS key exists on LocalStack Community.
  name                       = var.moderation_queue_name
  message_retention_seconds  = var.moderation_queue_retention_seconds
  visibility_timeout_seconds = var.moderation_queue_visibility_timeout
  receive_wait_time_seconds  = 5
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.moderation_dlq.arn
    maxReceiveCount     = var.moderation_max_receive_count
  })

  tags = {
    Name = var.moderation_queue_name
    role = "comment-moderation-queue"
  }
}

resource "aws_sqs_queue_redrive_allow_policy" "moderation_dlq" {
  queue_url = aws_sqs_queue.moderation_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.moderation.arn]
  })
}

#############################################
# CloudWatch Logs: blog-backend-logs
#############################################

resource "aws_cloudwatch_log_group" "backend" {
  # checkov:skip=CKV_AWS_158: log group uses default CloudWatch encryption; no customer managed KMS key is provisioned for LocalStack Community.
  name              = var.log_group_name
  retention_in_days = var.log_retention_in_days

  tags = {
    Name = "blog-backend-logs"
    role = "application-logs"
  }
}

#############################################
# IAM: least-privilege role + policy for the backend
#############################################

data "aws_iam_policy_document" "app_assume_role" {
  statement {
    sid     = "AllowComputeToAssumeBackendRole"
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
  description        = "Role assumed by the standalone blog platform FastAPI backend."
  assume_role_policy = data.aws_iam_policy_document.app_assume_role.json

  tags = {
    Name = var.app_role_name
  }
}

data "aws_iam_policy_document" "app" {
  statement {
    sid    = "PostsAndCommentsTableAccess"
    effect = "Allow"

    actions = [
      "dynamodb:DescribeTable",
      "dynamodb:GetItem",
      "dynamodb:BatchGetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan"
    ]

    resources = [
      aws_dynamodb_table.posts.arn,
      aws_dynamodb_table.published_comments.arn
    ]
  }

  statement {
    sid    = "ImagesBucketListing"
    effect = "Allow"

    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation"
    ]

    resources = [aws_s3_bucket.images.arn]
  }

  statement {
    sid    = "ImagesObjectAccess"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject"
    ]

    resources = ["${aws_s3_bucket.images.arn}/*"]
  }

  statement {
    sid    = "ModerationQueueAccess"
    effect = "Allow"

    actions = [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl"
    ]

    resources = [aws_sqs_queue.moderation.arn]
  }

  statement {
    sid    = "ModerationDlqInspection"
    effect = "Allow"

    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl"
    ]

    resources = [aws_sqs_queue.moderation_dlq.arn]
  }

  statement {
    sid    = "ApplicationLogging"
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
}

resource "aws_iam_policy" "app" {
  name        = var.app_policy_name
  description = "Least-privilege access for the blog backend to its DynamoDB tables, images bucket, moderation queues and log group."
  policy      = data.aws_iam_policy_document.app.json

  tags = {
    Name = var.app_policy_name
  }
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
