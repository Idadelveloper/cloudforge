output "dynamodb_table_name" {
  description = "Name of the DynamoDB table holding expense records."
  value       = aws_dynamodb_table.expenses.name
}

output "dynamodb_table_arn" {
  description = "ARN of the expenses DynamoDB table."
  value       = aws_dynamodb_table.expenses.arn
}

output "dynamodb_month_index_name" {
  description = "Name of the GSI used for month scoped queries and the summary endpoint."
  value       = var.month_index_name
}

output "dynamodb_category_index_name" {
  description = "Name of the GSI used for category filtered queries."
  value       = var.category_index_name
}

output "app_log_group_name" {
  description = "CloudWatch Logs group for application logs."
  value       = aws_cloudwatch_log_group.app.name
}

output "app_log_group_arn" {
  description = "ARN of the application CloudWatch Logs group."
  value       = aws_cloudwatch_log_group.app.arn
}

output "app_role_arn" {
  description = "ARN of the IAM role assumed by the expense tracker service."
  value       = aws_iam_role.app.arn
}

output "app_role_name" {
  description = "Name of the IAM role assumed by the expense tracker service."
  value       = aws_iam_role.app.name
}

output "app_policy_arn" {
  description = "ARN of the least-privilege IAM policy attached to the service role."
  value       = aws_iam_policy.app.arn
}

output "kms_key_arn" {
  description = "ARN of the customer managed key encrypting the table and logs."
  value       = aws_kms_key.main.arn
}

output "aws_region" {
  description = "Region the resources were deployed into."
  value       = var.aws_region
}
