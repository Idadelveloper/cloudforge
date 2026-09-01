output "events_table_name" {
  description = "DynamoDB table name for events (env var EVENTS_TABLE)."
  value       = aws_dynamodb_table.events.name
}

output "events_table_arn" {
  description = "ARN of the events DynamoDB table."
  value       = aws_dynamodb_table.events.arn
}

output "registrations_table_name" {
  description = "DynamoDB table name for registrations (env var REGISTRATIONS_TABLE)."
  value       = aws_dynamodb_table.registrations.name
}

output "registrations_table_arn" {
  description = "ARN of the registrations DynamoDB table."
  value       = aws_dynamodb_table.registrations.arn
}

output "registrations_email_index_name" {
  description = "Name of the GSI used for duplicate-registration detection."
  value       = var.registrations_email_index_name
}

output "registration_queue_url" {
  description = "URL of the registration events SQS queue (env var REGISTRATION_QUEUE_URL)."
  value       = aws_sqs_queue.registration_events.url
}

output "registration_queue_arn" {
  description = "ARN of the registration events SQS queue."
  value       = aws_sqs_queue.registration_events.arn
}

output "registration_dlq_url" {
  description = "URL of the registration events dead-letter queue."
  value       = aws_sqs_queue.registration_events_dlq.url
}

output "registration_dlq_arn" {
  description = "ARN of the registration events dead-letter queue."
  value       = aws_sqs_queue.registration_events_dlq.arn
}

output "service_role_arn" {
  description = "ARN of the least-privilege IAM role for the service."
  value       = aws_iam_role.service.arn
}

output "service_role_name" {
  description = "Name of the least-privilege IAM role for the service."
  value       = aws_iam_role.service.name
}

output "log_group_name" {
  description = "CloudWatch Logs group used by the application."
  value       = aws_cloudwatch_log_group.service.name
}

output "aws_region" {
  description = "Region the resources were created in."
  value       = var.aws_region
}
