data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_id    = data.aws_caller_identity.current.account_id
  region        = data.aws_region.current.name
  log_group_arn = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:${var.log_group_name}"
  media_prefix  = "${var.s3_object_prefix}/*"
}

#############################################
# KMS customer-managed key
# Encrypts both S3 buckets, both DynamoDB
# tables, the application log group and the
# runtime configuration secret.
#############################################
resource "aws_kms_key" "gallery" {
  description             = "CMK for ${var.project_name} S3 objects, DynamoDB tables, logs and secrets"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableAccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AllowCloudWatchLogsUse"
        Effect    = "Allow"
        Principal = { Service = "logs.${local.region}.amazonaws.com" }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncryptFrom",
          "kms:ReEncryptTo",
          "kms:GenerateDataKey",
          "kms:GenerateDataKeyWithoutPlaintext",
          "kms:DescribeKey"
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "${local.log_group_arn}"
          }
        }
      },
      {
        Sid       = "AllowS3ServerAccessLogDeliveryUse"
        Effect    = "Allow"
        Principal = { Service = "logging.s3.amazonaws.com" }
        Action = [
          "kms:GenerateDataKey",
          "kms:Decrypt"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
      }
    ]
  })
}

resource "aws_kms_alias" "gallery" {
  name          = "alias/${var.project_name}"
  target_key_id = aws_kms_key.gallery.key_id
}

#############################################
# S3 access-log bucket (target for the media
# bucket's server access logging)
#############################################
resource "aws_s3_bucket" "logs" {
  #checkov:skip=CKV_AWS_18:This bucket is itself the server-access-log destination; logging it to itself would recurse.
  #checkov:skip=CKV_AWS_144:Cross-region replication is not required for LocalStack access logs.
  #checkov:skip=CKV2_AWS_62:Access-log deliveries do not drive application event notifications.
  bucket        = var.logs_bucket_name
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
      kms_master_key_id = aws_kms_key.gallery.arn
      sse_algorithm     = "aws:kms"
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
      days = var.access_log_expiration_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "logs_bucket" {
  statement {
    sid    = "AllowS3ServerAccessLogDelivery"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.logs.arn}/${var.media_bucket_name}/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.logs.arn,
      "${aws_s3_bucket.logs.arn}/*"
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "logs" {
  bucket = aws_s3_bucket.logs.id
  policy = data.aws_iam_policy_document.logs_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.logs]
}

#############################################
# S3 media bucket: image-gallery-media
#############################################
resource "aws_s3_bucket" "media" {
  #checkov:skip=CKV_AWS_144:Single-region LocalStack deployment; cross-region replication is out of scope.
  #checkov:skip=CKV2_AWS_62:The application confirms uploads via head_object; no event-notification consumer exists in the plan.
  bucket        = var.media_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_ownership_controls" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "media" {
  bucket = aws_s3_bucket.media.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.gallery.arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_logging" "media" {
  bucket        = aws_s3_bucket.media.id
  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "${var.media_bucket_name}/"
}

resource "aws_s3_bucket_lifecycle_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    id     = "cleanup-noncurrent-and-incomplete-uploads"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  cors_rule {
    id              = "browser-direct-upload"
    allowed_methods = ["GET", "PUT", "POST", "HEAD", "DELETE"]
    allowed_origins = var.cors_allowed_origins
    allowed_headers = ["*"]
    expose_headers  = ["ETag", "Content-Type", "Content-Length"]
    max_age_seconds = 3000
  }
}

data "aws_iam_policy_document" "media_bucket" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.media.arn,
      "${aws_s3_bucket.media.arn}/*"
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "media" {
  bucket = aws_s3_bucket.media.id
  policy = data.aws_iam_policy_document.media_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.media]
}

#############################################
# DynamoDB: albums and images metadata
#############################################
resource "aws_dynamodb_table" "albums" {
  name         = var.albums_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "album_id"

  attribute {
    name = "album_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.gallery.arn
  }

  tags = {
    Name = var.albums_table_name
  }
}

resource "aws_dynamodb_table" "images" {
  name         = var.images_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "album_id"
  range_key    = "image_id"

  attribute {
    name = "album_id"
    type = "S"
  }

  attribute {
    name = "image_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.gallery.arn
  }

  tags = {
    Name = var.images_table_name
  }
}

#############################################
# CloudWatch Logs: /cloudforge/image-gallery
#############################################
resource "aws_cloudwatch_log_group" "app" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.gallery.arn

  depends_on = [aws_kms_key.gallery]
}

#############################################
# Secrets Manager: image-gallery/app-config
#############################################
resource "aws_secretsmanager_secret" "app_config" {
  #checkov:skip=CKV2_AWS_57:Configuration values are not credentials; automatic rotation would require a rotation Lambda not present in the plan.
  name        = var.app_config_secret_name
  description = "Runtime configuration for the ${var.project_name} backend (bucket, table names, presigned URL TTL)."
  kms_key_id  = aws_kms_key.gallery.arn
}

resource "aws_secretsmanager_secret_version" "app_config" {
  secret_id = aws_secretsmanager_secret.app_config.id

  secret_string = jsonencode({
    aws_region                = var.aws_region
    media_bucket              = aws_s3_bucket.media.bucket
    s3_object_prefix          = var.s3_object_prefix
    albums_table              = aws_dynamodb_table.albums.name
    images_table              = aws_dynamodb_table.images.name
    presigned_url_ttl_seconds = var.presigned_url_ttl_seconds
    log_group_name            = aws_cloudwatch_log_group.app.name
  })
}

#############################################
# IAM: image-gallery-app-role
#############################################
data "aws_iam_policy_document" "app_assume_role" {
  statement {
    sid     = "AllowServiceAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = [var.app_role_trusted_service]
    }
  }
}

resource "aws_iam_role" "app" {
  name                 = var.app_role_name
  description          = "Least-privilege role for the ${var.project_name} FastAPI backend"
  assume_role_policy   = data.aws_iam_policy_document.app_assume_role.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "app_s3" {
  statement {
    sid    = "ListMediaBucketObjects"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation"
    ]
    resources = [aws_s3_bucket.media.arn]
  }

  statement {
    sid    = "ManageAlbumImageObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:AbortMultipartUpload"
    ]
    resources = ["${aws_s3_bucket.media.arn}/${local.media_prefix}"]
  }
}

data "aws_iam_policy_document" "app_dynamodb" {
  statement {
    sid    = "AlbumAndImageMetadataAccess"
    effect = "Allow"
    actions = [
      "dynamodb:DescribeTable",
      "dynamodb:GetItem",
      "dynamodb:BatchGetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:Query",
      "dynamodb:Scan"
    ]
    resources = [
      aws_dynamodb_table.albums.arn,
      aws_dynamodb_table.images.arn
    ]
  }
}

data "aws_iam_policy_document" "app_observability" {
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

  statement {
    sid    = "ReadRuntimeConfiguration"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret"
    ]
    resources = [aws_secretsmanager_secret.app_config.arn]
  }

  statement {
    sid    = "UseGalleryCmk"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncryptFrom",
      "kms:ReEncryptTo",
      "kms:GenerateDataKey",
      "kms:DescribeKey"
    ]
    resources = [aws_kms_key.gallery.arn]
  }
}

resource "aws_iam_policy" "app_s3" {
  name        = "${var.app_role_name}-s3"
  description = "S3 access for image upload, download and deletion in the media bucket"
  policy      = data.aws_iam_policy_document.app_s3.json
}

resource "aws_iam_policy" "app_dynamodb" {
  name        = "${var.app_role_name}-dynamodb"
  description = "DynamoDB CRUD/Query access on the albums and images tables"
  policy      = data.aws_iam_policy_document.app_dynamodb.json
}

resource "aws_iam_policy" "app_observability" {
  name        = "${var.app_role_name}-observability"
  description = "CloudWatch Logs writes, config secret reads and CMK usage"
  policy      = data.aws_iam_policy_document.app_observability.json
}

resource "aws_iam_role_policy_attachment" "app_s3" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app_s3.arn
}

resource "aws_iam_role_policy_attachment" "app_dynamodb" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app_dynamodb.arn
}

resource "aws_iam_role_policy_attachment" "app_observability" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app_observability.arn
}
