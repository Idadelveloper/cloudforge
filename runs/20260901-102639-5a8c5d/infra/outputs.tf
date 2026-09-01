output "aws_region" {
  description = "Region the notification hub resources live in."
  value       = var.aws_region
}

output "sns_topic_arn" {
  description = "ARN of the central notification hub SNS topic (SNS_TOPIC_ARN)."
  value       = aws_sns_topic.events.arn
}

output "sns_topic_name" {
  description = "Name of the central notification hub SNS topic."
  value       = aws_sns_topic.events.name
}

output "email_queue_url" {
  description = "SQS queue URL backing the 'email' channel (EMAIL_QUEUE_URL)."
  value       = aws_sqs_queue.channel["email"].url
}

output "email_queue_arn" {
  description = "ARN of the 'email' channel queue."
  value       = aws_sqs_queue.channel["email"].arn
}

output "webhook_queue_url" {
  description = "SQS queue URL backing the 'webhook' channel (WEBHOOK_QUEUE_URL)."
  value       = aws_sqs_queue.channel["webhook"].url
}

output "webhook_queue_arn" {
  description = "ARN of the 'webhook' channel queue."
  value       = aws_sqs_queue.channel["webhook"].arn
}

output "channel_queue_urls" {
  description = "Map of channel name to its backing SQS queue URL."
  value       = { for name, queue in aws_sqs_queue.channel : name => queue.url }
}

output "dead_letter_queue_url" {
  description = "URL of the shared dead-letter queue."
  value       = aws_sqs_queue.dlq.url
}

output "dead_letter_queue_arn" {
  description = "ARN of the shared dead-letter queue."
  value       = aws_sqs_queue.dlq.arn
}

output "dynamodb_table_name" {
  description = "Name of the subscriptions DynamoDB table (SUBSCRIPTIONS_TABLE)."
  value       = aws_dynamodb_table.subscriptions.name
}

output "dynamodb_table_arn" {
  description = "ARN of the subscriptions DynamoDB table."
  value       = aws_dynamodb_table.subscriptions.arn
}

output "dynamodb_channel_index_name" {
  description = "Name of the channel/created_at global secondary index."
  value       = var.dynamodb_channel_index_name
}

output "log_group_name" {
  description = "CloudWatch Logs group used for application logging."
  value       = aws_cloudwatch_log_group.application.name
}

output "service_role_arn" {
  description = "ARN of the IAM role the notification hub backend assumes."
  value       = aws_iam_role.service.arn
}

output "service_policy_arn" {
  description = "ARN of the least-privilege policy attached to the service role."
  value       = aws_iam_policy.service.arn
}

output "kms_key_arn" {
  description = "ARN of the customer managed key protecting the hub's data."
  value       = aws_kms_key.notification_hub.arn
}
