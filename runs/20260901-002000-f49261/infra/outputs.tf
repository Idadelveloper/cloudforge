output "aws_region" {
  description = "Region the expense tracker resources were created in (AWS_REGION for the app)."
  value       = var.aws_region
}

output "expenses_table_name" {
  description = "DynamoDB table name for expense records (EXPENSES_TABLE for the app)."
  value       = aws_dynamodb_table.expenses.name
}

output "expenses_table_arn" {
  description = "ARN of the DynamoDB expenses table."
  value       = aws_dynamodb_table.expenses.arn
}

output "expenses_table_hash_key" {
  description = "Partition key of the expenses table."
  value       = aws_dynamodb_table.expenses.hash_key
}

output "expenses_table_range_key" {
  description = "Sort key of the expenses table ('<date>#<expense_id>')."
  value       = aws_dynamodb_table.expenses.range_key
}

output "category_index_name" {
  description = "Name of the category global secondary index used for category-filtered queries."
  value       = var.category_index_name
}

output "category_index_arn" {
  description = "ARN of the category global secondary index."
  value       = "${aws_dynamodb_table.expenses.arn}/index/${var.category_index_name}"
}

output "app_role_name" {
  description = "Name of the IAM role for the expense tracker service."
  value       = aws_iam_role.app.name
}

output "app_role_arn" {
  description = "ARN of the IAM role for the expense tracker service."
  value       = aws_iam_role.app.arn
}

output "dynamodb_policy_arn" {
  description = "ARN of the least-privilege DynamoDB access policy."
  value       = aws_iam_policy.dynamodb_access.arn
}

output "log_group_name" {
  description = "CloudWatch log group name for application logs."
  value       = aws_cloudwatch_log_group.app.name
}

output "log_group_arn" {
  description = "CloudWatch log group ARN for application logs."
  value       = aws_cloudwatch_log_group.app.arn
}
