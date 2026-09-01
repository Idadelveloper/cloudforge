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

data "aws_region" "current" {}

########################################
# DynamoDB - primary feedback store
# Plan entry: DynamoDB / product-feedback
########################################
resource "aws_dynamodb_table" "feedback" {
  # checkov:skip=CKV_AWS_119: LocalStack Community does not provide customer-managed KMS CMKs; AWS-managed DynamoDB encryption is enabled below.
  name         = var.feedback_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = var.feedback_table_hash_key

  attribute {
    name = var.feedback_table_hash_key
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
    name            = var.feedback_product_index_name
    hash_key        = "product_id"
    range_key       = "created_at"
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
    Name      = var.feedback_table_name
    component = "feedback-store"
  }
}

########################################
# SNS - low rating alerts
# Plan entry: SNS / product-feedback-low-rating-alerts
########################################
resource "aws_sns_topic" "low_rating_alerts" {
  name              = var.low_rating_topic_name
  display_name      = "Product feedback low rating alerts"
  kms_master_key_id = var.sns_kms_master_key_id

  tags = {
    Name      = var.low_rating_topic_name
    component = "low-rating-alerts"
  }
}

########################################
# CloudWatch Logs - application logs
# Plan entry: CloudWatch / /cloudforge/product-feedback-service
########################################
resource "aws_cloudwatch_log_group" "app" {
  # checkov:skip=CKV_AWS_158: LocalStack Community deployment target has no customer-managed KMS CMK available for log group encryption.
  name              = var.log_group_name
  retention_in_days = var.log_retention_in_days

  tags = {
    Name      = var.log_group_name
    component = "observability"
  }
}

########################################
# IAM - least privilege service role
# Plan entry: IAM / product-feedback-service-role
########################################
data "aws_iam_policy_document" "service_assume_role" {
  statement {
    sid     = "AllowComputeServicesToAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "service_permissions" {
  statement {
    sid    = "FeedbackTableReadWrite"
    effect = "Allow"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:BatchGetItem",
      "dynamodb:DescribeTable",
    ]

    resources = [
      aws_dynamodb_table.feedback.arn,
      "${aws_dynamodb_table.feedback.arn}/index/${var.feedback_product_index_name}",
    ]
  }

  statement {
    sid    = "PublishLowRatingAlerts"
    effect = "Allow"

    actions = [
      "sns:Publish",
      "sns:GetTopicAttributes",
    ]

    resources = [aws_sns_topic.low_rating_alerts.arn]
  }

  statement {
    sid    = "WriteApplicationLogs"
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

resource "aws_iam_role" "service" {
  name                 = var.service_role_name
  description          = "Least-privilege role for the product feedback FastAPI service"
  assume_role_policy   = data.aws_iam_policy_document.service_assume_role.json
  max_session_duration = 3600

  tags = {
    Name      = var.service_role_name
    component = "service-identity"
  }
}

resource "aws_iam_policy" "service" {
  name        = "${var.service_role_name}-policy"
  description = "DynamoDB, SNS and CloudWatch Logs permissions scoped to the product feedback resources"
  policy      = data.aws_iam_policy_document.service_permissions.json

  tags = {
    Name      = "${var.service_role_name}-policy"
    component = "service-identity"
  }
}

resource "aws_iam_role_policy_attachment" "service" {
  role       = aws_iam_role.service.name
  policy_arn = aws_iam_policy.service.arn
}
