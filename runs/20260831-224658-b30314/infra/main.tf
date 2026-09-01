provider "aws" {
  region            = var.aws_region
  s3_use_path_style = var.s3_use_path_style

  default_tags {
    tags = {
      project    = var.project_name
      managed-by = "cloudforge-terraform"
    }
  }
}

locals {
  access_logs_bucket_name = "${var.s3_bucket_name}-access-logs"

  common_tags = {
    project     = var.project_name
    managed-by  = "cloudforge-terraform"
    environment = var.environment
  }
}

########################################
# S3 - access log bucket for the objects bucket
########################################

resource "aws_s3_bucket" "access_logs" {
  #checkov:skip=CKV_AWS_18:This bucket is itself the access-log target; self-logging would create a recursive log loop.
  #checkov:skip=CKV_AWS_144:Cross-region replication is not supported by LocalStack Community Edition.
  #checkov:skip=CKV_AWS_145:LocalStack Community deployment is limited to SSE-S3 (AES256); no KMS CMK is provisioned.
  #checkov:skip=CKV2_AWS_62:Event notifications are not used by this application; the plan contains no Lambda, SQS or SNS consumer.
  #checkov:skip=CKV2_AWS_61:A lifecycle configuration with expiration and noncurrent expiration rules is defined separately for this bucket.
  bucket        = local.access_logs_bucket_name
  force_destroy = true

  tags = merge(local.common_tags, {
    Name    = local.access_logs_bucket_name
    purpose = "s3-access-logs"
  })
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

resource "aws_s3_bucket_public_access_block" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
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

    expiration {
      days = var.access_log_expiration_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_s3_bucket_policy" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.access_logs.arn,
          "${aws_s3_bucket.access_logs.arn}/*"
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
      {
        Sid    = "AllowS3ServerAccessLogDelivery"
        Effect = "Allow"
        Principal = {
          Service = "logging.s3.amazonaws.com"
        }
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.access_logs.arn}/s3-access-logs/*"
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.access_logs]
}

########################################
# S3 - uploaded file objects bucket
########################################

resource "aws_s3_bucket" "objects" {
  #checkov:skip=CKV_AWS_144:Cross-region replication is not supported by LocalStack Community Edition.
  #checkov:skip=CKV_AWS_145:LocalStack Community deployment is limited to SSE-S3 (AES256); no KMS CMK is provisioned.
  #checkov:skip=CKV2_AWS_62:Event notifications are not used by this application; the plan contains no Lambda, SQS or SNS consumer.
  bucket        = var.s3_bucket_name
  force_destroy = true

  tags = merge(local.common_tags, {
    Name    = var.s3_bucket_name
    purpose = "user-uploaded-file-objects"
  })
}

resource "aws_s3_bucket_versioning" "objects" {
  bucket = aws_s3_bucket.objects.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "objects" {
  bucket = aws_s3_bucket.objects.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "objects" {
  bucket = aws_s3_bucket.objects.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "objects" {
  bucket = aws_s3_bucket.objects.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_logging" "objects" {
  bucket = aws_s3_bucket.objects.id

  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "s3-access-logs/"
}

resource "aws_s3_bucket_lifecycle_configuration" "objects" {
  bucket = aws_s3_bucket.objects.id

  rule {
    id     = "abort-incomplete-uploads-and-expire-noncurrent"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiration_days
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "objects" {
  bucket = aws_s3_bucket.objects.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "PUT", "HEAD", "DELETE"]
    allowed_origins = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

resource "aws_s3_bucket_policy" "objects" {
  bucket = aws_s3_bucket.objects.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.objects.arn,
          "${aws_s3_bucket.objects.arn}/*"
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.objects]
}

########################################
# DynamoDB - file metadata table
########################################

resource "aws_dynamodb_table" "file_metadata" {
  #checkov:skip=CKV_AWS_119:LocalStack Community deployment uses the AWS-owned DynamoDB encryption key; no KMS CMK is provisioned.
  name         = var.dynamodb_table_name
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
    name = "uploaded_at"
    type = "S"
  }

  global_secondary_index {
    name            = var.dynamodb_owner_index_name
    hash_key        = "owner"
    range_key       = "uploaded_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name    = var.dynamodb_table_name
    purpose = "file-metadata-store"
  })
}

########################################
# CloudWatch Logs - application log group
########################################

resource "aws_cloudwatch_log_group" "app" {
  #checkov:skip=CKV_AWS_158:LocalStack Community deployment does not provision a KMS CMK for log group encryption.
  name              = var.log_group_name
  retention_in_days = var.log_retention_in_days

  tags = merge(local.common_tags, {
    Name    = var.log_group_name
    purpose = "application-logs"
  })
}

########################################
# IAM - least privilege role for the backend service
########################################

data "aws_iam_policy_document" "assume_role" {
  statement {
    sid     = "AllowServiceAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com", "ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name                 = var.iam_role_name
  description          = "Least-privilege role used by the file sharing backend to access S3, DynamoDB and CloudWatch Logs."
  assume_role_policy   = data.aws_iam_policy_document.assume_role.json
  max_session_duration = 3600

  tags = merge(local.common_tags, {
    Name = var.iam_role_name
  })
}

data "aws_iam_policy_document" "app" {
  statement {
    sid    = "ListUploadBucket"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation"
    ]
    resources = [aws_s3_bucket.objects.arn]
  }

  statement {
    sid    = "ReadWriteUploadObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject"
    ]
    resources = ["${aws_s3_bucket.objects.arn}/*"]
  }

  statement {
    sid    = "FileMetadataTableAccess"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:DescribeTable"
    ]
    resources = [
      aws_dynamodb_table.file_metadata.arn,
      "${aws_dynamodb_table.file_metadata.arn}/index/${var.dynamodb_owner_index_name}"
    ]
  }

  statement {
    sid    = "WriteApplicationLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams"
    ]
    resources = [
      aws_cloudwatch_log_group.app.arn,
      "${aws_cloudwatch_log_group.app.arn}:*"
    ]
  }
}

resource "aws_iam_policy" "app" {
  name        = "${var.iam_role_name}-policy"
  description = "Least-privilege permissions for the file sharing backend (S3 objects, file metadata table, application log group)."
  policy      = data.aws_iam_policy_document.app.json

  tags = merge(local.common_tags, {
    Name = "${var.iam_role_name}-policy"
  })
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
