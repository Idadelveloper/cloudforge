provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project      = var.project_name
      "managed-by" = var.managed_by
    }
  }
}

############################################
# DynamoDB: events
############################################

resource "aws_dynamodb_table" "events" {
  # checkov:skip=CKV_AWS_119: LocalStack Community Edition does not support KMS customer managed keys; AWS-owned key encryption is enabled instead.
  name         = var.events_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"

  attribute {
    name = "event_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = {
    Name      = var.events_table_name
    component = "events-store"
  }
}

############################################
# DynamoDB: registrations
############################################

resource "aws_dynamodb_table" "registrations" {
  # checkov:skip=CKV_AWS_119: LocalStack Community Edition does not support KMS customer managed keys; AWS-owned key encryption is enabled instead.
  name         = var.registrations_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"
  range_key    = "registration_id"

  attribute {
    name = "event_id"
    type = "S"
  }

  attribute {
    name = "registration_id"
    type = "S"
  }

  attribute {
    name = "attendee_email"
    type = "S"
  }

  global_secondary_index {
    name            = var.registrations_email_index_name
    hash_key        = "attendee_email"
    range_key       = "event_id"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = {
    Name      = var.registrations_table_name
    component = "registrations-store"
  }
}

############################################
# SQS: registration events queue + DLQ
############################################

resource "aws_sqs_queue" "registration_events_dlq" {
  # checkov:skip=CKV_AWS_27: LocalStack Community Edition does not support KMS customer managed keys; SQS-managed server-side encryption is enabled.
  name                      = var.registration_dlq_name
  message_retention_seconds = var.dlq_message_retention_seconds
  sqs_managed_sse_enabled   = true

  tags = {
    Name      = var.registration_dlq_name
    component = "registration-events-dlq"
  }
}

resource "aws_sqs_queue" "registration_events" {
  # checkov:skip=CKV_AWS_27: LocalStack Community Edition does not support KMS customer managed keys; SQS-managed server-side encryption is enabled.
  name                       = var.registration_queue_name
  message_retention_seconds  = var.queue_message_retention_seconds
  visibility_timeout_seconds = var.queue_visibility_timeout_seconds
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.registration_events_dlq.arn
    maxReceiveCount     = var.queue_max_receive_count
  })

  tags = {
    Name      = var.registration_queue_name
    component = "registration-events-queue"
  }
}

resource "aws_sqs_queue_redrive_allow_policy" "registration_events_dlq" {
  queue_url = aws_sqs_queue.registration_events_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.registration_events.arn]
  })
}

############################################
# CloudWatch Logs: application log group
############################################

resource "aws_cloudwatch_log_group" "service" {
  # checkov:skip=CKV_AWS_158: LocalStack Community Edition does not support KMS customer managed keys for CloudWatch Logs.
  name              = var.log_group_name
  retention_in_days = var.log_retention_in_days

  tags = {
    Name      = var.log_group_name
    component = "application-logs"
  }
}

############################################
# IAM: least-privilege service role
############################################

data "aws_iam_policy_document" "service_assume_role" {
  statement {
    sid     = "AllowServiceRuntimeToAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "service" {
  name               = var.service_role_name
  description        = "Least-privilege role for the event registration service (DynamoDB + SQS + CloudWatch Logs)."
  assume_role_policy = data.aws_iam_policy_document.service_assume_role.json

  tags = {
    Name      = var.service_role_name
    component = "service-identity"
  }
}

data "aws_iam_policy_document" "service_permissions" {
  statement {
    sid    = "EventsTableAccess"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:DescribeTable"
    ]

    resources = [aws_dynamodb_table.events.arn]
  }

  statement {
    sid    = "RegistrationsTableAccess"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:DescribeTable"
    ]

    resources = [
      aws_dynamodb_table.registrations.arn,
      "${aws_dynamodb_table.registrations.arn}/index/${var.registrations_email_index_name}"
    ]
  }

  statement {
    sid    = "PublishRegistrationEvents"
    effect = "Allow"

    actions = [
      "sqs:SendMessage",
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes"
    ]

    resources = [aws_sqs_queue.registration_events.arn]
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
      aws_cloudwatch_log_group.service.arn,
      "${aws_cloudwatch_log_group.service.arn}:log-stream:*"
    ]
  }
}

resource "aws_iam_policy" "service" {
  name        = "${var.service_role_name}-policy"
  description = "Least-privilege permissions for the event registration service."
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
