output "aws_region" {
  description = "Region the stack was deployed into."
  value       = data.aws_region.current.name
}

output "account_id" {
  description = "AWS account id used for the deployment."
  value       = data.aws_caller_identity.current.account_id
}

output "feedback_table_name" {
  description = "DynamoDB table name; export as FEEDBACK_TABLE for the application."
  value       = aws_dynamodb_table.feedback.name
}

output "feedback_table_arn" {
  description = "ARN of the feedback DynamoDB table."
  value       = aws_dynamodb_table.feedback.arn
}

output "feedback_product_index_name" {
  description = "Name of the product_id/created_at global secondary index."
  value       = var.feedback_product_index_name
}

output "low_rating_topic_arn" {
  description = "SNS topic ARN; export as LOW_RATING_TOPIC_ARN for the application."
  value       = aws_sns_topic.low_rating_alerts.arn
}

output "low_rating_topic_name" {
  description = "SNS topic name for low-rating alerts."
  value       = aws_sns_topic.low_rating_alerts.name
}

output "service_role_arn" {
  description = "ARN of the least-privilege service role."
  value       = aws_iam_role.service.arn
}

output "service_policy_arn" {
  description = "ARN of the managed policy attached to the service role."
  value       = aws_iam_policy.service.arn
}

output "log_group_name" {
  description = "CloudWatch Logs group for application logs."
  value       = aws_cloudwatch_log_group.app.name
}
