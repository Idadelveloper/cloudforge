output "dynamodb_table_name" {
  description = "Name of the products table; supply to the application as DYNAMODB_TABLE_NAME."
  value       = aws_dynamodb_table.products.name
}

output "dynamodb_table_arn" {
  description = "ARN of the products table."
  value       = aws_dynamodb_table.products.arn
}

output "dynamodb_hash_key" {
  description = "Partition key attribute of the products table."
  value       = aws_dynamodb_table.products.hash_key
}

output "app_role_name" {
  description = "Name of the IAM role the application assumes."
  value       = aws_iam_role.app.name
}

output "app_role_arn" {
  description = "ARN of the IAM role the application assumes."
  value       = aws_iam_role.app.arn
}

output "app_policy_arn" {
  description = "ARN of the least-privilege policy attached to the application role."
  value       = aws_iam_policy.app_dynamodb.arn
}

output "application_log_group_name" {
  description = "CloudWatch log group name for application and stock-adjustment audit logs."
  value       = aws_cloudwatch_log_group.application.name
}

output "application_log_group_arn" {
  description = "ARN of the application CloudWatch log group."
  value       = aws_cloudwatch_log_group.application.arn
}

output "kms_key_arn" {
  description = "ARN of the customer managed key encrypting the products table and application logs."
  value       = aws_kms_key.inventory.arn
}

output "aws_region" {
  description = "Region the inventory resources were created in."
  value       = var.aws_region
}
