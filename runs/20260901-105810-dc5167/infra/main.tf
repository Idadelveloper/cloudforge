data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

###############################################################################
# DynamoDB - blog-posts
# Stores post items (markdown body, title, author, tags, status, image keys).
###############################################################################
resource "aws_dynamodb_table" "posts" {
  #checkov:skip=CKV_AWS_119:LocalStack Community has no KMS CMK support; DynamoDB SSE with the AWS owned key is used.
  name         = var.posts_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "post_id"

  attribute {
    name = "post_id"
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
    Name      = var.posts_table_name
    component = "posts"
  }
}

###############################################################################
# DynamoDB - blog-comments
# Published comments keyed by post_id (partition) + comment_id (sort).
###############################################################################
resource "aws_dynamodb_table" "comments" {
  #checkov:skip=CKV_AWS_119:LocalStack Community has no KMS CMK support; DynamoDB SSE with the AWS owned key is used.
  name         = var.comments_table_name
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

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }

  deletion_protection_enabled = false

  tags = {
    Name      = var.comments_table_name
    component = "comments"
  }
}

###############################################################################
# S3 - access log bucket for the post images bucket
###############################################################################
resource "aws_s3_bucket" "images_logs" {
  #checkov:skip=CKV_AWS_144:Cross-region replication is unavailable in LocalStack Community and not required by the plan.
  #checkov:skip=CKV_AWS_145:LocalStack Community has no KMS CMK support; SSE-S3 (AES256) is configured on this bucket instead.
  #checkov:skip=CKV2_AWS_62:Event notifications are not part of the planned architecture for the access log bucket.
  #checkov:skip=CKV_AWS_18:This bucket is the access log destination and logs to itself under a dedicated prefix.
  bucket        = var.images_log_bucket_name
  force_destroy = true

  tags = {
    Name      = var.images_log_bucket_name
    component = "post-images-access-logs"
  }
}

resource "aws_s3_bucket_ownership_controls" "images_logs" {
  bucket = aws_s3_bucket.images_logs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "images_logs" {
  bucket                  = aws_s3_bucket.images_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "images_logs" {
  bucket = aws_s3_bucket.images_logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "images_logs" {
  #checkov:skip=CKV_AWS_145:LocalStack Community has no KMS CMK support; SSE-S3 (AES256) is used.
  bucket = aws_s3_bucket.images_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_logging" "images_logs" {
  bucket        = aws_s3_bucket.images_logs.id
  target_bucket = aws_s3_bucket.images_logs.id
  target_prefix = "self-access-logs/"
}

resource "aws_s3_bucket_lifecycle_configuration" "images_logs" {
  bucket = aws_s3_bucket.images_logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {
      prefix = ""
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    expiration {
      days = var.log_object_expiration_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

###############################################################################
# S3 - blog-post-images
# Post image uploads (PutObject) and presigned GET download URLs.
###############################################################################
resource "aws_s3_bucket" "post_images" {
  #checkov:skip=CKV_AWS_144:Cross-region replication is unavailable in LocalStack Community and not required by the plan.
  #checkov:skip=CKV_AWS_145:LocalStack Community has no KMS CMK support; SSE-S3 (AES256) is configured on this bucket instead.
  #checkov:skip=CKV2_AWS_62:Image uploads are handled synchronously by the API; no S3 event consumer exists in the plan.
  bucket        = var.images_bucket_name
  force_destroy = true

  tags = {
    Name      = var.images_bucket_name
    component = "post-images"
  }
}

resource "aws_s3_bucket_ownership_controls" "post_images" {
  bucket = aws_s3_bucket.post_images.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "post_images" {
  bucket                  = aws_s3_bucket.post_images.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "post_images" {
  bucket = aws_s3_bucket.post_images.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "post_images" {
  #checkov:skip=CKV_AWS_145:LocalStack Community has no KMS CMK support; SSE-S3 (AES256) is used.
  bucket = aws_s3_bucket.post_images.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_logging" "post_images" {
  bucket        = aws_s3_bucket.post_images.id
  target_bucket = aws_s3_bucket.images_logs.id
  target_prefix = "post-images/"
}

resource "aws_s3_bucket_lifecycle_configuration" "post_images" {
  bucket = aws_s3_bucket.post_images.id

  rule {
    id     = "cleanup-image-versions-and-uploads"
    status = "Enabled"

    filter {
      prefix = ""
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    noncurrent_version_expiration {
      noncurrent_days = var.image_noncurrent_version_expiration_days
    }
  }
}

# Deny any non-TLS access to the images bucket (defence in depth for presigned URLs).
resource "aws_s3_bucket_policy" "post_images" {
  bucket = aws_s3_bucket.post_images.id
  policy = data.aws_iam_policy_document.post_images_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.post_images]
}

data "aws_iam_policy_document" "post_images_bucket" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.post_images.arn,
      "${aws_s3_bucket.post_images.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

###############################################################################
# SQS - blog-comment-moderation-dlq
###############################################################################
resource "aws_sqs_queue" "comment_moderation_dlq" {
  #checkov:skip=CKV_AWS_27:SQS managed server-side encryption (SSE-SQS) is enabled; KMS CMKs are unavailable in LocalStack Community.
  name                       = var.moderation_dlq_name
  message_retention_seconds  = var.moderation_message_retention_seconds
  visibility_timeout_seconds = var.moderation_visibility_timeout_seconds
  sqs_managed_sse_enabled    = true

  tags = {
    Name      = var.moderation_dlq_name
    component = "comment-moderation-dlq"
  }
}

###############################################################################
# SQS - blog-comment-moderation
###############################################################################
resource "aws_sqs_queue" "comment_moderation" {
  #checkov:skip=CKV_AWS_27:SQS managed server-side encryption (SSE-SQS) is enabled; KMS CMKs are unavailable in LocalStack Community.
  name                       = var.moderation_queue_name
  message_retention_seconds  = var.moderation_message_retention_seconds
  visibility_timeout_seconds = var.moderation_visibility_timeout_seconds
  receive_wait_time_seconds  = 2
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.comment_moderation_dlq.arn
    maxReceiveCount     = var.moderation_max_receive_count
  })

  tags = {
    Name      = var.moderation_queue_name
    component = "comment-moderation"
  }
}

resource "aws_sqs_queue_redrive_allow_policy" "comment_moderation_dlq" {
  queue_url = aws_sqs_queue.comment_moderation_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.comment_moderation.arn]
  })
}

###############################################################################
# CloudWatch - blog-backend-logs (log group + moderation backlog alarm)
###############################################################################
resource "aws_cloudwatch_log_group" "backend" {
  #checkov:skip=CKV_AWS_158:LocalStack Community has no KMS CMK support; the log group uses default CloudWatch encryption.
  name              = var.log_group_name
  retention_in_days = var.log_retention_days

  tags = {
    Name      = var.log_group_name
    component = "backend-logs"
  }
}

resource "aws_cloudwatch_log_stream" "moderation_decisions" {
  name           = "moderation-decisions"
  log_group_name = aws_cloudwatch_log_group.backend.name
}

resource "aws_cloudwatch_metric_alarm" "moderation_backlog" {
  #checkov:skip=CKV_AWS_319:No notification target is part of the plan; the alarm state is polled by the operator.
  alarm_name          = "${var.moderation_queue_name}-backlog"
  alarm_description   = "Comment moderation backlog exceeded the configured threshold of visible messages."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.moderation_backlog_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  actions_enabled     = true

  dimensions = {
    QueueName = aws_sqs_queue.comment_moderation.name
  }

  tags = {
    Name      = "${var.moderation_queue_name}-backlog"
    component = "backend-logs"
  }
}

###############################################################################
# IAM - blog-backend-service-role (least privilege for the backend process)
###############################################################################
data "aws_iam_policy_document" "backend_assume_role" {
  statement {
    sid     = "AllowComputeServiceToAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backend" {
  name                 = var.service_role_name
  description          = "Least-privilege role for the blog platform backend process."
  assume_role_policy   = data.aws_iam_policy_document.backend_assume_role.json
  max_session_duration = 3600

  tags = {
    Name      = var.service_role_name
    component = "backend-service-role"
  }
}

data "aws_iam_policy_document" "backend" {
  statement {
    sid    = "PostsTableAccess"
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

    resources = [aws_dynamodb_table.posts.arn]
  }

  statement {
    sid    = "CommentsTableAccess"
    effect = "Allow"

    actions = [
      "dynamodb:DescribeTable",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
    ]

    resources = [aws_dynamodb_table.comments.arn]
  }

  statement {
    sid    = "PostImageObjectAccess"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:GetObjectVersion",
      "s3:DeleteObjectVersion",
    ]

    resources = ["${aws_s3_bucket.post_images.arn}/*"]
  }

  statement {
    sid    = "PostImageBucketAccess"
    effect = "Allow"

    actions = [
      "s3:ListBucket",
      "s3:ListBucketVersions",
      "s3:GetBucketLocation",
    ]

    resources = [aws_s3_bucket.post_images.arn]
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
      "sqs:GetQueueUrl",
    ]

    resources = [
      aws_sqs_queue.comment_moderation.arn,
      aws_sqs_queue.comment_moderation_dlq.arn,
    ]
  }

  statement {
    sid    = "BackendLogWrite"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]

    resources = [
      aws_cloudwatch_log_group.backend.arn,
      "${aws_cloudwatch_log_group.backend.arn}:log-stream:*",
    ]
  }
}

resource "aws_iam_policy" "backend" {
  name        = "${var.service_role_name}-policy"
  description = "Least-privilege access to the blog posts/comments tables, images bucket, moderation queues and log group."
  policy      = data.aws_iam_policy_document.backend.json

  tags = {
    Name      = "${var.service_role_name}-policy"
    component = "backend-service-role"
  }
}

resource "aws_iam_role_policy_attachment" "backend" {
  role       = aws_iam_role.backend.name
  policy_arn = aws_iam_policy.backend.arn
}
