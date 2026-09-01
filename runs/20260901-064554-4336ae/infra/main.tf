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

#############################################
# S3 access log bucket (required so the documents
# bucket can have server access logging enabled)
#############################################

resource "aws_s3_bucket" "access_logs" {
  # checkov:skip=CKV_AWS_144:Cross-region replication is out of scope for a LocalStack Community deployment.
  # checkov:skip=CKV2_AWS_62:No event-driven consumers exist in this application (no Lambda/SQS/SNS in the plan).
  bucket = var.access_logs_bucket_name

  tags = {
    Name    = var.access_logs_bucket_name
    purpose = "s3-server-access-logs"
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
  # checkov:skip=CKV_AWS_145:LocalStack Community has limited KMS support; SSE-S3 (AES256) is used instead of a CMK.
  bucket = aws_s3_bucket.access_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
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
      days_after_initiation = var.abort_incomplete_multipart_days
    }
  }
}

resource "aws_s3_bucket_logging" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "self-access-logs/"
}

#############################################
# S3 documents bucket (plan: document-store-documents)
#############################################

resource "aws_s3_bucket" "documents" {
  # checkov:skip=CKV_AWS_144:Cross-region replication is out of scope for a LocalStack Community deployment.
  # checkov:skip=CKV2_AWS_62:No event-driven consumers exist in this application (no Lambda/SQS/SNS in the plan).
  bucket = var.documents_bucket_name

  tags = {
    Name    = var.documents_bucket_name
    purpose = "versioned-document-binaries"
  }
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket = aws_s3_bucket.documents.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  # checkov:skip=CKV_AWS_145:LocalStack Community has limited KMS support; SSE-S3 (AES256) is used instead of a CMK.
  bucket = aws_s3_bucket.documents.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    id     = "expire-old-document-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = var.abort_incomplete_multipart_days
    }
  }
}

resource "aws_s3_bucket_logging" "documents" {
  bucket = aws_s3_bucket.documents.id

  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "documents-bucket/"
}

resource "aws_s3_bucket_policy" "documents_tls_only" {
  bucket = aws_s3_bucket.documents.id
  policy = data.aws_iam_policy_document.documents_tls_only.json

  depends_on = [aws_s3_bucket_public_access_block.documents]
}

data "aws_iam_policy_document" "documents_tls_only" {
  statement {
    sid    = "DenyNonTlsRequests"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.documents.arn,
      "${aws_s3_bucket.documents.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

#############################################
# DynamoDB: document metadata (PK document_id, SK version)
#############################################

resource "aws_dynamodb_table" "document_metadata" {
  # checkov:skip=CKV_AWS_119:LocalStack Community has limited KMS support; DynamoDB SSE is enabled with the AWS-owned key.
  name         = var.metadata_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "document_id"
  range_key    = "version"

  attribute {
    name = "document_id"
    type = "S"
  }

  attribute {
    name = "version"
    type = "N"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }

  deletion_protection_enabled = false

  tags = {
    Name    = var.metadata_table_name
    purpose = "document-version-metadata"
  }
}

#############################################
# DynamoDB: tag index (PK tag, SK document_id)
#############################################

resource "aws_dynamodb_table" "document_tag_index" {
  # checkov:skip=CKV_AWS_119:LocalStack Community has limited KMS support; DynamoDB SSE is enabled with the AWS-owned key.
  name         = var.tag_index_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "tag"
  range_key    = "document_id"

  attribute {
    name = "tag"
    type = "S"
  }

  attribute {
    name = "document_id"
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
    Name    = var.tag_index_table_name
    purpose = "document-tag-search-index"
  }
}

#############################################
# CloudWatch Logs: /document-store/app
#############################################

resource "aws_cloudwatch_log_group" "app" {
  # checkov:skip=CKV_AWS_158:LocalStack Community has limited KMS support; log group uses default CloudWatch encryption.
  name              = var.log_group_name
  retention_in_days = var.log_retention_in_days

  tags = {
    Name    = var.log_group_name
    purpose = "application-request-and-audit-logs"
  }
}

#############################################
# IAM: document-store-app-role (least privilege)
#############################################

data "aws_iam_policy_document" "app_assume_role" {
  statement {
    sid     = "AllowEc2ServiceToAssume"
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
  description          = "Least-privilege role for the document-store FastAPI backend."
  assume_role_policy   = data.aws_iam_policy_document.app_assume_role.json
  max_session_duration = 3600

  tags = {
    Name = var.app_role_name
  }
}

data "aws_iam_policy_document" "app" {
  statement {
    sid    = "DocumentBucketListing"
    effect = "Allow"

    actions = [
      "s3:ListBucket",
      "s3:ListBucketVersions",
      "s3:GetBucketLocation",
      "s3:GetBucketVersioning",
    ]

    resources = [aws_s3_bucket.documents.arn]
  }

  statement {
    sid    = "DocumentObjectAccess"
    effect = "Allow"

    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:GetObjectAttributes",
      "s3:GetObjectVersionAttributes",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]

    resources = ["${aws_s3_bucket.documents.arn}/documents/*"]
  }

  statement {
    sid    = "DocumentMetadataTableAccess"
    effect = "Allow"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:DescribeTable",
    ]

    resources = [
      aws_dynamodb_table.document_metadata.arn,
      aws_dynamodb_table.document_tag_index.arn,
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
  description = "Least-privilege access to the document-store S3 bucket, DynamoDB tables and log group."
  policy      = data.aws_iam_policy_document.app.json
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
