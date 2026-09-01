##############################################
# DynamoDB - events
# Plan resource: DynamoDB / events
##############################################
resource "aws_dynamodb_table" "events" {
  name         = var.events_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"

  attribute {
    name = "event_id"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }

  deletion_protection_enabled = false

  tags = {
    Name      = var.events_table_name
    Component = "events-store"
  }
}

##############################################
# DynamoDB - registrations
# Plan resource: DynamoDB / registrations
##############################################
resource "aws_dynamodb_table" "registrations" {
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

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }

  deletion_protection_enabled = false

  tags = {
    Name      = var.registrations_table_name
    Component = "registrations-store"
  }
}

##############################################
# SQS - registration events dead-letter queue
# Plan resource: SQS / registration-events-dlq
##############################################
resource "aws_sqs_queue" "registration_events_dlq" {
  name                       = var.registration_dlq_name
  message_retention_seconds  = var.dlq_message_retention_seconds
  visibility_timeout_seconds = var.queue_visibility_timeout_seconds
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true

  tags = {
    Name      = var.registration_dlq_name
    Component = "registration-events-dlq"
  }
}

##############################################
# SQS - registration events queue
# Plan resource: SQS / registration-events
##############################################
resource "aws_sqs_queue" "registration_events" {
  name                       = var.registration_queue_name
  message_retention_seconds  = var.queue_message_retention_seconds
  visibility_timeout_seconds = var.queue_visibility_timeout_seconds
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.registration_events_dlq.arn
    maxReceiveCount     = var.queue_max_receive_count
  })

  tags = {
    Name      = var.registration_queue_name
    Component = "registration-events"
  }
}

resource "aws_sqs_queue_redrive_allow_policy" "registration_events_dlq" {
  queue_url = aws_sqs_queue.registration_events_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.registration_events.arn]
  })
}

##############################################
# CloudWatch Logs
# Plan resource: CloudWatch / /cloudforge/event-registration-service
##############################################
resource "aws_cloudwatch_log_group" "service" {
  # checkov:skip=CKV_AWS_158: LocalStack Community deployment target; KMS CMKs are outside the approved service set for this stack, log data is encrypted with the CloudWatch Logs service-managed key.
  name              = var.log_group_name
  retention_in_days = var.log_retention_in_days

  tags = {
    Name      = var.log_group_name
    Component = "application-logs"
  }
}

resource "aws_cloudwatch_log_metric_filter" "rejected_registrations" {
  name           = "${var.project_name}-rejected-registrations"
  log_group_name = aws_cloudwatch_log_group.service.name
  pattern        = "\"registration_rejected\""

  metric_transformation {
    name          = "RejectedRegistrations"
    namespace     = var.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "registration_queue_backlog" {
  alarm_name          = "${var.project_name}-registration-queue-backlog"
  alarm_description   = "Registration events queue depth is above the accepted backlog threshold."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.queue_depth_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.registration_events.name
  }
}

resource "aws_cloudwatch_metric_alarm" "registration_dlq_messages" {
  alarm_name          = "${var.project_name}-registration-dlq-messages"
  alarm_description   = "Registration messages are landing in the dead-letter queue."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.registration_events_dlq.name
  }
}

resource "aws_cloudwatch_metric_alarm" "rejected_registrations" {
  alarm_name          = "${var.project_name}-rejected-registrations"
  alarm_description   = "Elevated number of rejected registrations (event full or duplicate attendee)."
  namespace           = var.metric_namespace
  metric_name         = aws_cloudwatch_log_metric_filter.rejected_registrations.metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.rejected_registrations_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}

##############################################
# IAM - service role and least-privilege policy
# Plan resource: IAM / event-registration-service-role
##############################################
resource "aws_iam_role" "service" {
  name        = var.service_role_name
  description = "Least-privilege role for the event registration backend service."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ServiceAssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name      = var.service_role_name
    Component = "service-identity"
  }
}

resource "aws_iam_policy" "service" {
  name        = "${var.service_role_name}-policy"
  description = "Allows the event registration service to use its DynamoDB tables, registration queue and log group only."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EventsTableAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:ConditionCheckItem",
          "dynamodb:DescribeTable"
        ]
        Resource = [
          aws_dynamodb_table.events.arn
        ]
      },
      {
        Sid    = "RegistrationsTableAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:ConditionCheckItem",
          "dynamodb:DescribeTable"
        ]
        Resource = [
          aws_dynamodb_table.registrations.arn,
          "${aws_dynamodb_table.registrations.arn}/index/${var.registrations_email_index_name}"
        ]
      },
      {
        Sid    = "RegistrationQueuePublish"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:GetQueueUrl",
          "sqs:GetQueueAttributes"
        ]
        Resource = [
          aws_sqs_queue.registration_events.arn
        ]
      },
      {
        Sid    = "ApplicationLogging"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = [
          aws_cloudwatch_log_group.service.arn,
          "${aws_cloudwatch_log_group.service.arn}:log-stream:*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "service" {
  role       = aws_iam_role.service.name
  policy_arn = aws_iam_policy.service.arn
}
