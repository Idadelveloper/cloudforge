output "aws_region" {
  description = "Region the resources were created in; export as AWS_REGION for the service."
  value       = data.aws_region.current.name
}

output "account_id" {
  description = "AWS (or LocalStack) account id owning the resources."
  value       = data.aws_caller_identity.current.account_id
}

output "dynamodb_table_name" {
  description = "Task table name; export as TASKS_TABLE_NAME for the FastAPI service."
  value       = aws_dynamodb_table.todo_tasks.name
}

output "dynamodb_table_arn" {
  description = "ARN of the task table."
  value       = aws_dynamodb_table.todo_tasks.arn
}

output "dynamodb_hash_key" {
  description = "Partition key attribute name of the task table."
  value       = aws_dynamodb_table.todo_tasks.hash_key
}

output "application_log_group_name" {
  description = "CloudWatch Logs group name for application logs."
  value       = aws_cloudwatch_log_group.application.name
}

output "application_log_group_arn" {
  description = "CloudWatch Logs group ARN for application logs."
  value       = aws_cloudwatch_log_group.application.arn
}

output "app_role_arn" {
  description = "ARN of the IAM role the FastAPI service runs as."
  value       = aws_iam_role.app.arn
}

output "app_role_name" {
  description = "Name of the IAM role the FastAPI service runs as."
  value       = aws_iam_role.app.name
}

output "app_policy_arn" {
  description = "ARN of the least privilege application policy."
  value       = aws_iam_policy.app.arn
}
