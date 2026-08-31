output "tasks_table_name" {
  description = "Name of the DynamoDB tasks table (set as TASKS_TABLE for the application)."
  value       = aws_dynamodb_table.tasks.name
}

output "tasks_table_arn" {
  description = "ARN of the DynamoDB tasks table."
  value       = aws_dynamodb_table.tasks.arn
}

output "tasks_table_hash_key" {
  description = "Partition key attribute of the tasks table."
  value       = aws_dynamodb_table.tasks.hash_key
}

output "app_role_name" {
  description = "Name of the IAM role used by the todo_api service."
  value       = aws_iam_role.app.name
}

output "app_role_arn" {
  description = "ARN of the IAM role used by the todo_api service."
  value       = aws_iam_role.app.arn
}

output "app_policy_arn" {
  description = "ARN of the least-privilege policy attached to the application role."
  value       = aws_iam_policy.tasks_table_access.arn
}

output "aws_region" {
  description = "Region the resources were created in (set as AWS_REGION for the application)."
  value       = var.aws_region
}
