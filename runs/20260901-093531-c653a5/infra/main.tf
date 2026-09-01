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

data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name

  orders_table_arn = "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.orders_table_name}"
}

#############################################
# Customer managed KMS key
# Used to satisfy encryption-at-rest for the
# DynamoDB table, SQS queues, SNS topic, the
# Lambda environment and the log group.
#############################################

resource "aws_kms_key" "order_processing" {
  description             = "CMK protecting order processing data at rest"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  is_enabled              = true

  policy = jsonencode({
    Version = "2012-10-17"
    Id      = "order-processing-key-policy"
    Statement = [
      {
        Sid    = "EnableAccountAdministration"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowOrderProcessingServicesUseOfTheKey"
        Effect = "Allow"
        Principal = {
          Service = [
            "sns.amazonaws.com",
            "sqs.amazonaws.com",
            "dynamodb.amazonaws.com",
            "lambda.amazonaws.com"
          ]
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = "*"
      },
      {
        Sid    = "AllowCloudWatchLogsUseOfTheKey"
        Effect = "Allow"
        Principal = {
          Service = "logs.${local.region}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt*",
          "kms:Decrypt*",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*"
        ]
        Resource = "*"
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-cmk"
  }
}

resource "aws_kms_alias" "order_processing" {
  name          = "alias/${var.project_name}"
  target_key_id = aws_kms_key.order_processing.key_id
}

#############################################
# DynamoDB - orders table
#############################################

resource "aws_dynamodb_table" "orders" {
  name         = var.orders_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "order_id"

  attribute {
    name = "order_id"
    type = "S"
  }

  attribute {
    name = "customer_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = var.orders_customer_index_name
    hash_key        = "customer_id"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.order_processing.arn
  }

  tags = {
    Name = var.orders_table_name
  }
}

#############################################
# SQS - fulfilment queue + dead letter queue
#############################################

resource "aws_sqs_queue" "order_fulfilment_dlq" {
  name                              = var.fulfilment_dlq_name
  message_retention_seconds         = var.dlq_message_retention_seconds
  visibility_timeout_seconds        = var.queue_visibility_timeout_seconds
  kms_master_key_id                 = aws_kms_key.order_processing.arn
  kms_data_key_reuse_period_seconds = 300

  tags = {
    Name = var.fulfilment_dlq_name
  }
}

resource "aws_sqs_queue" "order_fulfilment" {
  name                              = var.fulfilment_queue_name
  message_retention_seconds         = var.queue_message_retention_seconds
  visibility_timeout_seconds        = var.queue_visibility_timeout_seconds
  receive_wait_time_seconds         = 10
  kms_master_key_id                 = aws_kms_key.order_processing.arn
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.order_fulfilment_dlq.arn
    maxReceiveCount     = var.queue_max_receive_count
  })

  tags = {
    Name = var.fulfilment_queue_name
  }
}

resource "aws_sqs_queue_redrive_allow_policy" "order_fulfilment_dlq" {
  queue_url = aws_sqs_queue.order_fulfilment_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.order_fulfilment.arn]
  })
}

#############################################
# SNS - order status topic
#############################################

resource "aws_sns_topic" "order_status" {
  name              = var.order_status_topic_name
  display_name      = "Order status changes"
  kms_master_key_id = aws_kms_key.order_processing.arn

  tags = {
    Name = var.order_status_topic_name
  }
}

#############################################
# CloudWatch Logs - fulfilment worker
#############################################

resource "aws_cloudwatch_log_group" "fulfilment_worker" {
  name              = var.fulfilment_worker_log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.order_processing.arn

  tags = {
    Name = var.fulfilment_worker_log_group_name
  }
}

#############################################
# IAM - fulfilment worker execution role
#############################################

resource "aws_iam_role" "fulfilment_worker" {
  name        = var.fulfilment_worker_role_name
  description = "Execution role for the order fulfilment Lambda worker"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "LambdaAssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = var.fulfilment_worker_role_name
  }
}

resource "aws_iam_policy" "fulfilment_worker" {
  name        = "${var.fulfilment_worker_role_name}-policy"
  description = "Least privilege permissions for the order fulfilment Lambda worker"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "WriteWorkerLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = [
          "${aws_cloudwatch_log_group.fulfilment_worker.arn}:*"
        ]
      },
      {
        Sid    = "ConsumeFulfilmentQueue"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:ChangeMessageVisibility"
        ]
        Resource = [
          aws_sqs_queue.order_fulfilment.arn
        ]
      },
      {
        Sid    = "SendFailedInvocationsToDlq"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage"
        ]
        Resource = [
          aws_sqs_queue.order_fulfilment_dlq.arn
        ]
      },
      {
        Sid    = "UpdateOrderRecords"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:UpdateItem"
        ]
        Resource = [
          aws_dynamodb_table.orders.arn
        ]
      },
      {
        Sid    = "PublishStatusEvents"
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = [
          aws_sns_topic.order_status.arn
        ]
      },
      {
        Sid    = "UseOrderProcessingKey"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey"
        ]
        Resource = [
          aws_kms_key.order_processing.arn
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "fulfilment_worker" {
  role       = aws_iam_role.fulfilment_worker.name
  policy_arn = aws_iam_policy.fulfilment_worker.arn
}

#############################################
# IAM - application (REST service) policy
#############################################

resource "aws_iam_policy" "app" {
  name        = var.app_policy_name
  description = "Least privilege permissions for the order processing REST service"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "OrdersTableAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query"
        ]
        Resource = [
          aws_dynamodb_table.orders.arn,
          "${aws_dynamodb_table.orders.arn}/index/${var.orders_customer_index_name}"
        ]
      },
      {
        Sid    = "EnqueueFulfilmentMessages"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:GetQueueUrl",
          "sqs:GetQueueAttributes"
        ]
        Resource = [
          aws_sqs_queue.order_fulfilment.arn
        ]
      },
      {
        Sid    = "PublishOrderStatusEvents"
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = [
          aws_sns_topic.order_status.arn
        ]
      },
      {
        Sid    = "UseOrderProcessingKey"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey"
        ]
        Resource = [
          aws_kms_key.order_processing.arn
        ]
      }
    ]
  })
}

#############################################
# Lambda - order fulfilment worker
#############################################

resource "local_file" "fulfilment_worker_source" {
  filename        = "${path.module}/build/handler.py"
  file_permission = "0644"

  content = <<PYTHON
"""Order fulfilment worker.

Consumes messages from the order fulfilment SQS queue, advances the order
status in DynamoDB and publishes the status change to the SNS topic.
"""

import datetime
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

TABLE_NAME = os.environ.get(