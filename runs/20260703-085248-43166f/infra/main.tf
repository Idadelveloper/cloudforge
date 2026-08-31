data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# KMS key used to encrypt the DynamoDB table and CloudWatch log group.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "notes" {
  description             = "CMK for encrypting ${var.project_name} data at rest"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableRootAccountAdmin"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AllowCloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${data.aws_region.current.name}.amazonaws.com" }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_kms_alias" "notes" {
  name          = "alias/${var.project_name}"
  target_key_id = aws_kms_key.notes.key_id
}

# ---------------------------------------------------------------------------
# DynamoDB table storing notes.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "notes" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "note_id"

  attribute {
    name = "note_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.notes.arn
  }
}

# ---------------------------------------------------------------------------
# CloudWatch log group for application/request logs.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "notes" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.notes.arn
}

# ---------------------------------------------------------------------------
# IAM execution role for the notes API with least-privilege access.
# ---------------------------------------------------------------------------
resource "aws_iam_role" "notes_api" {
  name = var.iam_role_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "notes_dynamodb" {
  name = "${var.iam_role_name}-dynamodb"
  role = aws_iam_role.notes_api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "NotesTableCrud"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan"
        ]
        Resource = [
          aws_dynamodb_table.notes.arn
        ]
      },
      {
        Sid    = "NotesTableKmsUse"
        Effect = "Allow"
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = [
          aws_kms_key.notes.arn
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "notes_logs" {
  name = "${var.iam_role_name}-logs"
  role = aws_iam_role.notes_api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "WriteApiLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = [
          "${aws_cloudwatch_log_group.notes.arn}:*"
        ]
      }
    ]
  })
}
