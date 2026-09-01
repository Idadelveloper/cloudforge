data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

############################################################
# S3 - server access log bucket for the files bucket
############################################################

resource "aws_s3_bucket" "access_logs" {
  #checkov:skip=CKV_AWS_144:Cross-region replication is not available in LocalStack Community and is out of scope for this service.
  #checkov:skip=CKV_AWS_18:This bucket is the access-log target; it logs to itself via aws_s3_bucket_logging.access_logs.
  #checkov:skip=CKV2_AWS_62:S3 event notifications are not used; no queue worker or event consumer exists in the plan.
  #checkov:skip=CKV_AWS_145:KMS is outside the supported LocalStack Community service set; SSE-S3 (AES256) is used instead.
  bucket        = var.access_logs_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_ownership_controls" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

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
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_logging" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

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
      days_after_initiation = var.abort_incomplete_multipart_days
    }

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_retention_days
    }

    expiration {
      days = 90
    }
  }

  depends_on = [aws_s3_bucket_versioning.access_logs]
}

data "aws_iam_policy_document" "access_logs_bucket" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.access_logs.arn,
      "${aws_s3_bucket.access_logs.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid    = "AllowS3ServerAccessLogDelivery"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.access_logs.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_s3_bucket_policy" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id
  policy = data.aws_iam_policy_document.access_logs_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.access_logs]
}

############################################################
# S3 - uploaded file objects (presigned PUT/GET target)
############################################################

resource "aws_s3_bucket" "files" {
  #checkov:skip=CKV_AWS_144:Cross-region replication is not available in LocalStack Community and is out of scope for this service.
  #checkov:skip=CKV2_AWS_62:S3 event notifications are not used; uploads are confirmed by an explicit API call, not by S3 events.
  #checkov:skip=CKV_AWS_145:KMS is outside the supported LocalStack Community service set; SSE-S3 (AES256) is used instead.
  bucket        = var.files_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_ownership_controls" "files" {
  bucket = aws_s3_bucket.files.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "files" {
  bucket = aws_s3_bucket.files.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "files" {
  bucket = aws_s3_bucket.files.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "files" {
  bucket = aws_s3_bucket.files.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_logging" "files" {
  bucket = aws_s3_bucket.files.id

  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "file-share-files/"
}

resource "aws_s3_bucket_lifecycle_configuration" "files" {
  bucket = aws_s3_bucket.files.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = var.abort_incomplete_multipart_days
    }
  }

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_retention_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.files]
}

resource "aws_s3_bucket_cors_configuration" "files" {
  bucket = aws_s3_bucket.files.id

  cors_rule {
    allowed_methods = ["PUT", "GET", "HEAD", "DELETE"]
    allowed_origins = var.cors_allowed_origins
    allowed_headers = ["*"]
    expose_headers  = ["ETag", "Content-Length", "Content-Type"]
    max_age_seconds = 3000
  }
}

data "aws_iam_policy_document" "files_bucket" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.files.arn,
      "${aws_s3_bucket.files.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "files" {
  bucket = aws_s3_bucket.files.id
  policy = data.aws_iam_policy_document.files_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.files]
}

############################################################
# DynamoDB - file metadata table with owner GSI
############################################################

resource "aws_dynamodb_table" "metadata" {
  #checkov:skip=CKV_AWS_119:Customer-managed KMS keys are outside the supported LocalStack Community service set; AWS-managed encryption at rest is enabled.
  name         = var.metadata_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "file_id"

  attribute {
    name = "file_id"
    type = "S"
  }

  attribute {
    name = "owner"
    type = "S"
  }

  attribute {
    name = "upload_time"
    type = "S"
  }

  global_secondary_index {
    name            = var.owner_index_name
    hash_key        = "owner"
    range_key       = "upload_time"
    projection_type = "ALL"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }

  deletion_protection_enabled = false

  tags = {
    Name = var.metadata_table_name
  }
}

############################################################
# CloudWatch Logs - application log group
############################################################

resource "aws_cloudwatch_log_group" "app" {
  #checkov:skip=CKV_AWS_158:KMS is outside the supported LocalStack Community service set; logs use the default CloudWatch encryption.
  name              = var.log_group_name
  retention_in_days = var.log_retention_days

  tags = {
    Name = var.log_group_name
  }
}

############################################################
# IAM - least-privilege application role
############################################################

data "aws_iam_policy_document" "app_assume_role" {
  statement {
    sid     = "AllowServiceHostToAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name                 = var.app_role_name
  description          = "Role assumed by the file-share backend service to access S3, DynamoDB and CloudWatch Logs."
  assume_role_policy   = data.aws_iam_policy_document.app_assume_role.json
  max_session_duration = 3600

  tags = {
    Name = var.app_role_name
  }
}

data "aws_iam_policy_document" "app" {
  statement {
    sid    = "FileObjectAccess"
    effect = "Allow"

    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
    ]

    resources = ["${aws_s3_bucket.files.arn}/*"]
  }

  statement {
    sid    = "FileBucketListing"
    effect = "Allow"

    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
      "s3:ListBucketMultipartUploads",
    ]

    resources = [aws_s3_bucket.files.arn]
  }

  statement {
    sid    = "MetadataTableAccess"
    effect = "Allow"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:DescribeTable",
    ]

    resources = [
      aws_dynamodb_table.metadata.arn,
      "${aws_dynamodb_table.metadata.arn}/index/${var.owner_index_name}",
    ]
  }

  statement {
    sid    = "ApplicationLogging"
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
}

resource "aws_iam_policy" "app" {
  name        = var.app_policy_name
  description = "Least-privilege access for the file-share backend to its S3 bucket, DynamoDB table/GSI and log group."
  policy      = data.aws_iam_policy_document.app.json
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
