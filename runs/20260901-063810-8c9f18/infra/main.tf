data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  name_prefix = var.project_name

  common_tags = {
    application = "product_feedback_service"
    environment = var.environment
  }
}

# ---------------------------------------------------------------------------
# Customer managed KMS key: encrypts the DynamoDB table, the SNS topic and the
# CloudWatch log group listed in the plan.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "feedback" {
  description             = "CMK for product feedback service data at rest (DynamoDB, SNS, CloudWatch Logs)"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  key_usage               = "ENCRYPT_DECRYPT"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableAccountKeyAdministration"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action = [
          "kms:Create*",
          "kms:Describe*",
          "kms:Enable*",
          "kms:List*",
          "kms:Put*",
          "kms:Update*",
          "kms:Revoke*",
          "kms:Disable*",
          "kms:Get*",
          "kms:Delete*",
          "kms:TagResource",
          "kms:UntagResource",
          "kms:ScheduleKeyDeletion",
          "kms:CancelKeyDeletion",
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*"
        ]
        Resource = "*"
      },
      {
        Sid    = "AllowCloudWatchLogsUseOfKey"
        Effect = "Allow"
        Principal = {
          Service = "logs.${data.aws_region.current.name}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt*",
          "kms:Decrypt*",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*"
        ]
        Resource = "*"
      },
      {
        Sid    = "AllowSnsUseOfKey"
        Effect = "Allow"
        Principal = {
          Service = "sns.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = "*"
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-cmk"
  })
}

resource "aws_kms_alias" "feedback" {
  name          = "alias/${local.name_prefix}"
  target_key_id = aws_kms_key.feedback.key_id
}

# ---------------------------------------------------------------------------
# DynamoDB: primary store for feedback records (plan: DynamoDB product-feedback)
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "feedback" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "feedback_id"

  attribute {
    name = "feedback_id"
    type = "S"
  }

  attribute {
    name = "product_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = var.dynamodb_gsi_name
    hash_key        = "product_id"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.feedback.arn
  }

  deletion_protection_enabled = false

  tags = merge(local.common_tags, {
    Name = var.dynamodb_table_name
  })
}

# ---------------------------------------------------------------------------
# SNS: low rating (1-2 star) alert topic (plan: SNS low-rating-alerts)
# ---------------------------------------------------------------------------
resource "aws_sns_topic" "low_rating_alerts" {
  name              = var.sns_topic_name
  display_name      = "Product feedback low rating alerts"
  kms_master_key_id = aws_kms_key.feedback.arn

  tags = merge(local.common_tags, {
    Name = var.sns_topic_name
  })
}

# ---------------------------------------------------------------------------
# CloudWatch Logs: application logs (plan: /product-feedback/service-logs)
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "service" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_in_days
  kms_key_id        = aws_kms_key.feedback.arn

  tags = merge(local.common_tags, {
    Name = var.log_group_name
  })
}

# ---------------------------------------------------------------------------
# IAM: least-privilege role/policy for the service
# (plan: IAM product-feedback-service-role)
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "service_assume_role" {
  statement {
    sid     = "AllowComputeToAssumeServiceRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "service" {
  name                 = var.service_role_name
  description          = "Least-privilege role used by the product feedback service"
  assume_role_policy   = data.aws_iam_policy_document.service_assume_role.json
  max_session_duration = 3600

  tags = merge(local.common_tags, {
    Name = var.service_role_name
  })
}

data "aws_iam_policy_document" "service_permissions" {
  statement {
    sid    = "FeedbackTableAccess"
    effect = "Allow"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:DescribeTable"
    ]

    resources = [
      aws_dynamodb_table.feedback.arn,
      "${aws_dynamodb_table.feedback.arn}/index/${var.dynamodb_gsi_name}"
    ]
  }

  statement {
    sid    = "LowRatingAlertPublish"
    effect = "Allow"

    actions = [
      "sns:Publish",
      "sns:GetTopicAttributes"
    ]

    resources = [aws_sns_topic.low_rating_alerts.arn]
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
      aws_cloudwatch_log_group.service.arn,
      "${aws_cloudwatch_log_group.service.arn}:log-stream:*"
    ]
  }

  statement {
    sid    = "UseServiceCmk"
    effect = "Allow"

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncryptFrom",
      "kms:ReEncryptTo",
      "kms:GenerateDataKey",
      "kms:GenerateDataKeyWithoutPlaintext",
      "kms:DescribeKey"
    ]

    resources = [aws_kms_key.feedback.arn]
  }
}

resource "aws_iam_policy" "service" {
  name        = "${var.service_role_name}-policy"
  description = "Least-privilege access to the product-feedback table, low-rating-alerts topic and service log group"
  policy      = data.aws_iam_policy_document.service_permissions.json

  tags = merge(local.common_tags, {
    Name = "${var.service_role_name}-policy"
  })
}

resource "aws_iam_role_policy_attachment" "service" {
  role       = aws_iam_role.service.name
  policy_arn = aws_iam_policy.service.arn
}
