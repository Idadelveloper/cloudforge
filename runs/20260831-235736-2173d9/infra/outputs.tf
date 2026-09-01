output "events_table_name" {
  description = "Name of the DynamoDB table holding events."
  value       = aws_dynamodb_table.events.name
}

output "events_table_arn" {
  description = "ARN of the DynamoDB events table."
  value       = aws_dynamodb_table.events.arn
}

output "registrations_table_name" {
  description = "Name of the DynamoDB table holding registrations."
  value       = aws_dynamodb_table.registrations.name
}

output "registrations_table_arn" {
  description = "ARN of the DynamoDB registrations table."
  value       = aws_dynamodb_table.registrations.arn
}

output "registrations_email_index_name" {
  description = "Name of the GSI used for duplicate attendee-email checks."
  value       = var.registrations_email_index_name
}

output "registration_queue_name" {
  description = "Name of the registration events SQS queue."
  value       = aws_sqs_queue.registration_events.name
}

output "registration_queue_url" {
  description = "URL of the registration events SQS queue used by the application."
  value       = aws_sqs_queue.registration_events.id
}

output "registration_queue_arn" {
  description = "ARN of the registration events SQS queue."
  value       = aws_sqs_queue.registration_events.arn
}

output "registration_dlq_url" {
  description = "URL of the registration events dead-letter queue."
  value       = aws_sqs_queue.registration_events_dlq.id
}

output "registration_dlq_arn" {
  description = "ARN of the registration events dead-letter queue."
  value       = aws_sqs_queue.registration_events_dlq.arn
}

output "service_role_name" {
  description = "Name of the IAM role for the backend service."
  value       = aws_iam_role.service.name
}

output "service_role_arn" {
  description = "ARN of the IAM role for the backend service."
  value       = aws_iam_role.service.arn
}

output "service_policy_arn" {
  description = "ARN of the least-privilege policy attached to the service role."
  value       = aws_iam_policy.service.arn
}

output "log_group_name" {
  description = "CloudWatch Logs group used for application logs."
  value       = aws_cloudwatch_log_group.service.name
}

output "log_group_arn" {
  description = "ARN of the application CloudWatch Logs group."
  value       = aws_cloudwatch_log_group.service.arn
}
