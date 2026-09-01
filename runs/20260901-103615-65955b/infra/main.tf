provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project      = var.project_name
      "managed-by" = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  # Fixed set of delivery channels supported by the notification hub.
  # Each channel maps to a dedicated SQS queue subscribed to the central SNS topic.
  channel_queue_names = {
    email   = var.email_queue_name
    webhook = var.webhook_queue_name
  }
}

###############################################################################
# SNS - notification-hub-events
# Central topic that services publish events to via POST /events.
###############################################################################

resource "aws_sns_topic" "events" {
  name = var.topic_name

  # Server side encryption with the AWS managed key for SNS.
  kms_master_key_id = "alias/aws/sns"

  tags = {
    Name      = var.topic_name
    component = "events-topic"
  }
}

###############################################################################
# SQS - notification-hub-dlq
# Dead letter queue shared by both channel queues via redrive policy.
###############################################################################

resource "aws_sqs_queue" "dlq" {
  name = var.dlq_name

  sqs_managed_sse_enabled    = true
  message_retention_seconds  = 1209600
  visibility_timeout_seconds = var.queue_visibility_timeout_seconds

  tags = {
    Name      = var.dlq_name
    component = "dead-letter-queue"
  }
}

###############################################################################
# SQS - notification-hub-email-queue / notification-hub-webhook-queue
# One queue per delivery channel, read by GET /channels/{channel}/messages
# and measured by GET /stats.
###############################################################################

resource "aws_sqs_queue" "channel" {
  for_each = local.channel_queue_names

  name = each.value

  sqs_managed_sse_enabled    = true
  message_retention_seconds  = var.channel_queue_retention_seconds
  visibility_timeout_seconds = var.queue_visibility_timeout_seconds
  receive_wait_time_seconds  = 0
  delay_seconds              = 0
  max_message_size           = 262144

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = {
    Name      = each.value
    component = "channel-queue"
    channel   = each.key
  }
}

###############################################################################
# IAM - notification-hub-queue-policies
# Allow the SNS topic principal to deliver messages into each channel queue.
###############################################################################

data "aws_iam_policy_document" "channel_queue" {
  for_each = aws_sqs_queue.channel

  statement {
    sid    = "AllowNotificationHubTopicToSendMessages"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }

    actions   = ["sqs:SendMessage"]
    resources = [each.value.arn]

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_sns_topic.events.arn]
    }
  }

  statement {
    sid    = "AllowAccountOwnerQueueAdministration"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }

    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:ChangeMessageVisibility",
    ]

    resources = [each.value.arn]
  }
}

resource "aws_sqs_queue_policy" "channel" {
  for_each = aws_sqs_queue.channel

  queue_url = each.value.url
  policy    = data.aws_iam_policy_document.channel_queue[each.key].json
}

###############################################################################
# SNS subscriptions - notification-hub-email-subscription /
#                     notification-hub-webhook-subscription
# Wire each channel queue to the central topic with raw message delivery.
###############################################################################

resource "aws_sns_topic_subscription" "channel" {
  for_each = aws_sqs_queue.channel

  topic_arn            = aws_sns_topic.events.arn
  protocol             = "sqs"
  endpoint             = each.value.arn
  raw_message_delivery = true

  depends_on = [aws_sqs_queue_policy.channel]
}

###############################################################################
# DynamoDB - notification-hub-subscriptions
# Subscription records created/read/deleted by the /subscriptions endpoints.
###############################################################################

resource "aws_dynamodb_table" "subscriptions" {
  # checkov:skip=CKV_AWS_119: LocalStack Community does not provide KMS customer managed CMKs; the table uses DynamoDB owned/AWS managed encryption at rest.
  name         = var.subscriptions_table_name
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

  global_secondary_index {
    name            = var.subscriptions_channel_index_name
    hash_key        = "channel"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = {
    Name      = var.subscriptions_table_name
    component = "subscriptions-store"
  }
}

###############################################################################
# CloudWatch - notification-hub-logs
# Application log group for publish / subscription tracing.
###############################################################################

resource "aws_cloudwatch_log_group" "app" {
  # checkov:skip=CKV_AWS_158: LocalStack Community does not provide KMS CMK backed log group encryption; default encryption at rest is used.
  name              = var.log_group_name
  retention_in_days = var.log_retention_in_days

  tags = {
    Name      = var.log_group_name
    component = "application-logs"
  }
}

###############################################################################
# IAM - notification-hub-app-policy (+ role for the backend service)
# Least privilege access to exactly the resources declared above.
###############################################################################

data "aws_iam_policy_document" "app_assume_role" {
  statement {
    sid     = "AllowComputeServiceToAssumeHubRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name               = var.app_role_name
  description        = "Execution role for the notification hub FastAPI service"
  assume_role_policy = data.aws_iam_policy_document.app_assume_role.json

  tags = {
    Name      = var.app_role_name
    component = "application-role"
  }
}

data "aws_iam_policy_document" "app" {
  statement {
    sid    = "PublishAndInspectNotificationTopic"
    effect = "Allow"

    actions = [
      "sns:Publish",
      "sns:GetTopicAttributes",
      "sns:ListSubscriptionsByTopic",
    ]

    resources = [aws_sns_topic.events.arn]
  }

  statement {
    sid    = "ConsumeChannelQueues"
    effect = "Allow"

    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:ChangeMessageVisibility",
    ]

    resources = concat(
      [for q in aws_sqs_queue.channel : q.arn],
      [aws_sqs_queue.dlq.arn],
    )
  }

  statement {
    sid    = "ManageSubscriptionRecords"
    effect = "Allow"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:DescribeTable",
    ]

    resources = [
      aws_dynamodb_table.subscriptions.arn,
      "${aws_dynamodb_table.subscriptions.arn}/index/${var.subscriptions_channel_index_name}",
    ]
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

resource "aws_iam_policy" "app" {
  name        = var.app_policy_name
  description = "Least privilege policy for the notification hub service (SNS publish, SQS consume, DynamoDB subscriptions, CloudWatch logs)"
  policy      = data.aws_iam_policy_document.app.json

  tags = {
    Name      = var.app_policy_name
    component = "application-policy"
  }
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
