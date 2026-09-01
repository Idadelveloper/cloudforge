output "sns_topic_arn" {
  description = "ARN of the central notification hub SNS topic."
  value       = aws_sns_topic.events.arn
}

output "sns_topic_name" {
  description = "Name of the central notification hub SNS topic."
  value       = aws_sns_topic.events.name
}

output "channel_queue_urls" {
  description = "Map of channel name to backing SQS queue URL."
  value       = { for channel, queue in aws_sqs_queue.channel : channel => queue.url }
}

output "channel_queue_arns" {
  description = "Map of channel name to backing SQS queue ARN."
  value       = { for channel, queue in aws_sqs_queue.channel : channel => queue.arn }
}

output "channel_queue_names" {
  description = "Map of channel name to backing SQS queue name."
  value       = { for channel, queue in aws_sqs_queue.channel : channel => queue.name }
}

output "email_queue_url" {
  description = "URL of the email channel queue."
  value       = aws_sqs_queue.channel["email"].url
}

output "email_queue_arn" {
  description = "ARN of the email channel queue."
  value       = aws_sqs_queue.channel["email"].arn
}

output "webhook_queue_url" {
  description = "URL of the webhook channel queue."
  value       = aws_sqs_queue.channel["webhook"].url
}

output "webhook_queue_arn" {
  description = "ARN of the webhook channel queue."
  value       = aws_sqs_queue.channel["webhook"].arn
}

output "dlq_url" {
  description = "URL of the shared dead letter queue."
  value       = aws_sqs_queue.dlq.url
}

output "dlq_arn" {
  description = "ARN of the shared dead letter queue."
  value       = aws_sqs_queue.dlq.arn
}

output "subscriptions_table_name" {
  description = "Name of the DynamoDB table holding subscription records."
  value       = aws_dynamodb_table.subscriptions.name
}

output "subscriptions_table_arn" {
  description = "ARN of the DynamoDB table holding subscription records."
  value       = aws_dynamodb_table.subscriptions.arn
}

output "subscriptions_channel_index_name" {
  description = "Name of the DynamoDB global secondary index used for channel lookups."
  value       = var.subscriptions_channel_index_name
}

output "sns_subscription_arns" {
  description = "Map of channel name to the SNS-to-SQS subscription ARN."
  value       = { for channel, sub in aws_sns_topic_subscription.channel : channel => sub.arn }
}

output "log_group_name" {
  description = "CloudWatch Logs group used by the notification hub application."
  value       = aws_cloudwatch_log_group.app.name
}

output "app_role_arn" {
  description = "ARN of the IAM role for the notification hub backend."
  value       = aws_iam_role.app.arn
}

output "app_policy_arn" {
  description = "ARN of the least privilege IAM policy for the notification hub backend."
  value       = aws_iam_policy.app.arn
}
