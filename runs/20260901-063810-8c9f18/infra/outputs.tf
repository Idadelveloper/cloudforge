output "aws_region" {
  description = "Region the resources were created in."
  value       = var.aws_region
}

output "dynamodb_table_name" {
  description = "Name of the feedback DynamoDB table (FEEDBACK_TABLE_NAME)."
  value       = aws_dynamodb_table.feedback.name
}

output "dynamodb_table_arn" {
  description = "ARN of the feedback DynamoDB table."
  value       = aws_dynamodb_table.feedback.arn
}

output "dynamodb_gsi_name" {
  description = "Name of the product_id/created_at global secondary index (FEEDBACK_TABLE_GSI)."
  value       = var.dynamodb_gsi_name
}

output "sns_topic_arn" {
  description = "ARN of the low-rating alert SNS topic (LOW_RATING_TOPIC_ARN)."
  value       = aws_sns_topic.low_rating_alerts.arn
}

output "sns_topic_name" {
  description = "Name of the low-rating alert SNS topic."
  value       = aws_sns_topic.low_rating_alerts.name
}

output "cloudwatch_log_group_name" {
  description = "CloudWatch Logs group used for application logs (LOG_GROUP_NAME)."
  value       = aws_cloudwatch_log_group.service.name
}

output "service_role_arn" {
  description = "ARN of the least-privilege IAM role for the service."
  value       = aws_iam_role.service.arn
}

output "service_policy_arn" {
  description = "ARN of the IAM policy attached to the service role."
  value       = aws_iam_policy.service.arn
}

output "kms_key_arn" {
  description = "ARN of the customer managed key encrypting feedback data."
  value       = aws_kms_key.feedback.arn
}
