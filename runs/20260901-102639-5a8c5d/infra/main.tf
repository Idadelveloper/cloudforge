provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project      = var.project_name
      "managed-by" = "terraform"
      environment  = var.environment
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  channel_queue_names = {
    email   = var.email_queue_name
    webhook = var.webhook_queue_name
  }
}

########################################
# Customer managed KMS key
# Encrypts the SNS topic, the SQS queues, the DynamoDB table
# and the CloudWatch log group used by the notification hub.
########################################

resource "aws_kms_key" "notification_hub" {
  description             = "CMK protecting notification hub SNS, SQS, DynamoDB and CloudWatch Logs data"
  deletion_window_in_days = var.kms_deletion_window_in_days
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableAccountAdministration"
        Effect = "Allow"
        Principal = {
          AWS = "arn:${local.partition}:iam::${local.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowSnsAndSqsServiceUse"
        Effect = "Allow"
        Principal = {
          Service = [
            "sns.amazonaws.com",
            "sqs.amazonaws.com"
          ]
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:GenerateDataKey*"
        ]
        Resource = "*"
      },
      {
        Sid    = "AllowCloudWatchLogsUse"
        Effect = "Allow"
        Principal = {
          Service = "logs.${var.aws_region}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt*",
          "kms:Decrypt*",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*"
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:${local.partition}:logs:${var.aws_region}:${local.account_id}:log-group:*"
          }
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-cmk"
  }
}

resource "aws_kms_alias" "notification_hub" {
  name          = "alias/${var.project_name}"
  target_key_id = aws_kms_key.notification_hub.key_id
}

########################################
# Central SNS topic (fan-out point for POST /events)
########################################

resource "aws_sns_topic" "events" {
  name              = var.sns_topic_name
  kms_master_key_id = aws_kms_key.notification_hub.key_id

  tags = {
    Name = var.sns_topic_name
  }
}

########################################
# Shared dead-letter queue
########################################

resource "aws_sqs_queue" "dlq" {
  name                              = var.dlq_name
  message_retention_seconds         = var.dlq_message_retention_seconds
  visibility_timeout_seconds        = var.queue_visibility_timeout_seconds
  kms_master_key_id                 = aws_kms_key.notification_hub.key_id
  kms_data_key_reuse_period_seconds = 300

  tags = {
    Name = var.dlq_name
  }
}

########################################
# Per-channel SQS queues (email + webhook)
########################################

resource "aws_sqs_queue" "channel" {
  for_each = local.channel_queue_names

  name                              = each.value
  message_retention_seconds         = var.queue_message_retention_seconds
  visibility_timeout_seconds        = var.queue_visibility_timeout_seconds
  receive_wait_time_seconds         = 0
  kms_master_key_id                 = aws_kms_key.notification_hub.key_id
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = {
    Name    = each.value
    channel = each.key
  }
}

########################################
# Queue policies allowing the SNS topic to deliver to each queue
########################################

resource "aws_sqs_queue_policy" "channel" {
  for_each = aws_sqs_queue.channel

  queue_url = each.value.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowSnsTopicDelivery"
        Effect = "Allow"
        Principal = {
          Service = "sns.amazonaws.com"
        }
        Action   = "sqs:SendMessage"
        Resource = each.value.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_sns_topic.events.arn
          }
        }
      }
    ]
  })
}

########################################
# SNS -> SQS subscriptions (raw message delivery)
########################################

resource "aws_sns_topic_subscription" "channel" {
  for_each = aws_sqs_queue.channel

  topic_arn            = aws_sns_topic.events.arn
  protocol             = "sqs"
  endpoint             = each.value.arn
  raw_message_delivery = true

  depends_on = [aws_sqs_queue_policy.channel]
}

########################################
# DynamoDB table holding subscription records
########################################

resource "aws_dynamodb_table" "subscriptions" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "subscription_id"

  attribute {
    name = "subscription_id"
    type = "S"
  }

  attribute {
    name = "channel"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = var.dynamodb_channel_index_name
    hash_key        = "channel"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.notification_hub.arn
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = false
  }

  deletion_protection_enabled = false

  tags = {
    Name = var.dynamodb_table_name
  }
}

########################################
# CloudWatch log group for application logs
########################################

resource "aws_cloudwatch_log_group" "application" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.notification_hub.arn

  tags = {
    Name = var.log_group_name
  }
}

########################################
# IAM role + least-privilege policy for the backend service
########################################

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
  description          = "Role used by the notification hub backend to access SNS, SQS, DynamoDB and CloudWatch Logs"
  assume_role_policy   = data.aws_iam_policy_document.service_assume_role.json
  max_session_duration = 3600

  tags = {
    Name = var.service_role_name
  }
}

data "aws_iam_policy_document" "service" {
  statement {
    sid    = "PublishEventsToTopic"
    effect = "Allow"
    actions = [
      "sns:Publish",
      "sns:GetTopicAttributes",
      "sns:ListSubscriptionsByTopic"
    ]
    resources = [aws_sns_topic.events.arn]
  }

  statement {
    sid    = "ReadAndDrainChannelQueues"
    effect = "Allow"
    actions = [
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility"
    ]
    resources = concat(
      [for queue in aws_sqs_queue.channel : queue.arn],
      [aws_sqs_queue.dlq.arn]
    )
  }

  statement {
    sid    = "SubscriptionTableCrud"
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
      "dynamodb:DescribeTable"
    ]
    resources = [
      aws_dynamodb_table.subscriptions.arn,
      "${aws_dynamodb_table.subscriptions.arn}/index/${var.dynamodb_channel_index_name}"
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
    resources = [aws_cloudwatch_log_group.application.arn]
  }

  statement {
    sid    = "UseNotificationHubKey"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey"
    ]
    resources = [aws_kms_key.notification_hub.arn]
  }
}

resource "aws_iam_policy" "service" {
  name        = var.service_policy_name
  description = "Least-privilege access for the notification hub service to its own SNS topic, SQS queues, DynamoDB table and log group"
  policy      = data.aws_iam_policy_document.service.json

  tags = {
    Name = var.service_policy_name
  }
}

resource "aws_iam_role_policy_attachment" "service" {
  role       = aws_iam_role.service.name
  policy_arn = aws_iam_policy.service.arn
}
