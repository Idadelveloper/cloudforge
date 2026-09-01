data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
}

#############################################
# S3 - server access log bucket (supports the
# documents bucket logging requirement)
#############################################

resource "aws_s3_bucket" "access_logs" {
  # checkov:skip=CKV_AWS_144:LocalStack Community Edition does not support cross-region replication for this single-region log bucket.
  # checkov:skip=CKV_AWS_145:LocalStack Community Edition target; SSE-S3 (AES256) is used instead of a customer managed KMS key.
  # checkov:skip=CKV2_AWS_62:Access log bucket is write-only storage; no event notification consumer exists in this application.
  bucket        = var.access_logs_bucket_name
  force_destroy = true
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

resource "aws_s3_bucket_lifecycle_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.access_log_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.access_logs]
}

resource "aws_s3_bucket_logging" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "self-access-logs/"
}

#############################################
# S3 - versioned bucket for document binaries
#############################################

resource "aws_s3_bucket" "documents" {
  # checkov:skip=CKV_AWS_144:LocalStack Community Edition target; cross-region replication is unavailable and not required by the specification.
  # checkov:skip=CKV_AWS_145:LocalStack Community Edition target; SSE-S3 (AES256) is used instead of a customer managed KMS key.
  # checkov:skip=CKV2_AWS_62:The application reads objects synchronously via boto3; no event notification consumer is part of the plan.
  bucket        = var.documents_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket = aws_s3_bucket.documents.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
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
    id     = "retain-document-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.documents]
}

resource "aws_s3_bucket_logging" "documents" {
  bucket = aws_s3_bucket.documents.id

  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "documents-bucket/"
}

resource "aws_s3_bucket_ownership_controls" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

#############################################
# DynamoDB - document version metadata index
#############################################

resource "aws_dynamodb_table" "documents_metadata" {
  # checkov:skip=CKV_AWS_119:LocalStack Community Edition target; DynamoDB SSE is enabled with the AWS owned/managed key rather than a customer managed CMK.
  name         = var.dynamodb_table_name
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

  attribute {
    name = "tag"
    type = "S"
  }

  attribute {
    name = "author"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = var.tag_index_name
    hash_key        = "tag"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = var.author_index_name
    hash_key        = "author"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  deletion_protection_enabled = false
}

#############################################
# CloudWatch Logs - application log group
#############################################

resource "aws_cloudwatch_log_group" "app" {
  # checkov:skip=CKV_AWS_158:LocalStack Community Edition target; CloudWatch Logs KMS CMK encryption is not available, default service encryption applies.
  name              = var.log_group_name
  retention_in_days = var.log_retention_days
}

#############################################
# Secrets Manager - application configuration
#############################################

resource "random_password" "api_key" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "app_config" {
  # checkov:skip=CKV_AWS_149:LocalStack Community Edition target; the secret uses the default Secrets Manager encryption key.
  # checkov:skip=CKV2_AWS_57:Static service API key for a local/dev deployment; no rotation Lambda is part of the plan.
  name                    = var.app_config_secret_name
  description             = "Document-store service configuration: write API key and presigned URL settings."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "app_config" {
  secret_id = aws_secretsmanager_secret.app_config.id

  secret_string = jsonencode({
    api_key                             = random_password.api_key.result
    presigned_url_default_expiry_seconds = var.presigned_url_default_expiry_seconds
    presigned_url_max_expiry_seconds     = var.presigned_url_max_expiry_seconds
    max_upload_size_bytes                = var.max_upload_size_bytes
    documents_bucket                     = aws_s3_bucket.documents.bucket
    metadata_table                       = aws_dynamodb_table.documents_metadata.name
    tag_index                            = var.tag_index_name
    author_index                         = var.author_index_name
    log_group                            = aws_cloudwatch_log_group.app.name
  })
}

#############################################
# IAM - least privilege service role
#############################################

data "aws_iam_policy_document" "service_assume_role" {
  statement {
    sid     = "AllowAccountPrincipalsToAssumeServiceRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
  }
}

resource "aws_iam_role" "service" {
  name               = var.service_role_name
  description        = "Role assumed by the document-store FastAPI backend to access S3, DynamoDB, CloudWatch Logs and Secrets Manager."
  assume_role_policy = data.aws_iam_policy_document.service_assume_role.json
}

data "aws_iam_policy_document" "service" {
  statement {
    sid    = "DocumentBucketObjectAccess"
    effect = "Allow"

    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:GetObjectVersionAttributes",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion"
    ]

    resources = ["${aws_s3_bucket.documents.arn}/*"]
  }

  statement {
    sid    = "DocumentBucketListAccess"
    effect = "Allow"

    actions = [
      "s3:ListBucket",
      "s3:ListBucketVersions",
      "s3:GetBucketLocation",
      "s3:GetBucketVersioning"
    ]

    resources = [aws_s3_bucket.documents.arn]
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
      "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:DescribeTable"
    ]

    resources = [aws_dynamodb_table.documents_metadata.arn]
  }

  statement {
    sid    = "MetadataIndexQueryAccess"
    effect = "Allow"

    actions = [
      "dynamodb:Query"
    ]

    resources = [
      "${aws_dynamodb_table.documents_metadata.arn}/index/${var.tag_index_name}",
      "${aws_dynamodb_table.documents_metadata.arn}/index/${var.author_index_name}"
    ]
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
      aws_cloudwatch_log_group.app.arn,
      "${aws_cloudwatch_log_group.app.arn}:log-stream:*"
    ]
  }

  statement {
    sid    = "ReadApplicationConfigSecret"
    effect = "Allow"

    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret"
    ]

    resources = [aws_secretsmanager_secret.app_config.arn]
  }
}

resource "aws_iam_policy" "service" {
  name        = var.service_policy_name
  description = "Least privilege policy for the document-store backend service."
  policy      = data.aws_iam_policy_document.service.json
}

resource "aws_iam_role_policy_attachment" "service" {
  role       = aws_iam_role.service.name
  policy_arn = aws_iam_policy.service.arn
}
