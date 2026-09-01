data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
}

############################################
# KMS customer managed key
# Encrypts the DynamoDB tables, the SNS alert
# topic and the CloudWatch log group listed in
# the plan's aws_resources.
############################################

data "aws_iam_policy_document" "telemetry_key" {
  statement {
    sid    = "EnableAccountAdministration"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }

    actions   = ["kms:*"]
    resources = ["*"]
  }

  statement {
    sid    = "AllowCloudWatchLogsUseOfTheKey"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logs.${local.region}.amazonaws.com"]
    }

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncryptFrom",
      "kms:ReEncryptTo",
      "kms:GenerateDataKey",
      "kms:GenerateDataKeyWithoutPlaintext",
      "kms:DescribeKey",
    ]

    resources = ["*"]
  }

  statement {
    sid    = "AllowSnsUseOfTheKey"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }

    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]

    resources = ["*"]
  }
}

resource "aws_kms_key" "telemetry" {
  description             = "CMK for ${var.app_name} DynamoDB tables, SNS alerts and CloudWatch logs"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  is_enabled              = true
  policy                  = data.aws_iam_policy_document.telemetry_key.json

  tags = {
    Name = "${var.project}-iot-telemetry-cmk"
  }
}

resource "aws_kms_alias" "telemetry" {
  name          = "alias/${var.project}-iot-telemetry"
  target_key_id = aws_kms_key.telemetry.key_id
}

############################################
# DynamoDB: device registry (iot-devices)
############################################

resource "aws_dynamodb_table" "devices" {
  name         = var.devices_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "device_id"

  attribute {
    name = "device_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.telemetry.arn
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = false
  }

  tags = {
    Name = var.devices_table_name
    role = "device-registry"
  }
}

############################################
# DynamoDB: telemetry readings (iot-readings)
############################################

resource "aws_dynamodb_table" "readings" {
  name         = var.readings_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "device_id"
  range_key    = "timestamp"

  attribute {
    name = "device_id"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "S"
  }

  attribute {
    name = "day"
    type = "S"
  }

  local_secondary_index {
    name            = "device_day_index"
    range_key       = "day"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.telemetry.arn
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = false
  }

  tags = {
    Name = var.readings_table_name
    role = "telemetry-readings"
  }
}

############################################
# SNS: threshold breach alerts
############################################

resource "aws_sns_topic" "alerts" {
  name              = var.alerts_topic_name
  display_name      = "IoT telemetry threshold alerts"
  kms_master_key_id = aws_kms_key.telemetry.arn

  tags = {
    Name = var.alerts_topic_name
    role = "telemetry-alerts"
  }
}

data "aws_iam_policy_document" "alerts_topic" {
  statement {
    sid    = "AllowAccountPublishAndSubscribe"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }

    actions = [
      "sns:Publish",
      "sns:Subscribe",
      "sns:GetTopicAttributes",
      "sns:ListSubscriptionsByTopic",
    ]

    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts_topic.json
}

############################################
# CloudWatch Logs: application log group
############################################

resource "aws_cloudwatch_log_group" "app" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.telemetry.arn

  tags = {
    Name = var.log_group_name
    role = "application-logs"
  }
}

############################################
# IAM: least-privilege role for the backend
############################################

data "aws_iam_policy_document" "app_assume_role" {
  statement {
    sid     = "AllowComputeServiceAssumeRole"
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
  description          = "Least-privilege role for the ${var.app_name} FastAPI service"
  assume_role_policy   = data.aws_iam_policy_document.app_assume_role.json
  max_session_duration = 3600

  tags = {
    Name = var.app_role_name
  }
}

data "aws_iam_policy_document" "app_permissions" {
  statement {
    sid    = "DeviceRegistryAccess"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:BatchGetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:DescribeTable",
    ]

    resources = [
      aws_dynamodb_table.devices.arn,
    ]
  }

  statement {
    sid    = "ReadingsTableAccess"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:BatchGetItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:DescribeTable",
    ]

    resources = [
      aws_dynamodb_table.readings.arn,
      "${aws_dynamodb_table.readings.arn}/index/device_day_index",
    ]
  }

  statement {
    sid    = "PublishThresholdAlerts"
    effect = "Allow"

    actions = [
      "sns:Publish",
      "sns:GetTopicAttributes",
    ]

    resources = [aws_sns_topic.alerts.arn]
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

  statement {
    sid    = "UseTelemetryCmk"
    effect = "Allow"

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]

    resources = [aws_kms_key.telemetry.arn]
  }
}

resource "aws_iam_policy" "app" {
  name        = "${var.app_role_name}-policy"
  description = "Least-privilege access to the IoT telemetry DynamoDB tables, SNS alert topic, log group and CMK"
  policy      = data.aws_iam_policy_document.app_permissions.json
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
