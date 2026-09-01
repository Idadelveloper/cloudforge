output "products_table_name" {
  description = "Name of the DynamoDB table holding product records; set as the app's table configuration."
  value       = aws_dynamodb_table.products.name
}

output "products_table_arn" {
  description = "ARN of the products DynamoDB table."
  value       = aws_dynamodb_table.products.arn
}

output "products_table_hash_key" {
  description = "Partition key attribute of the products table."
  value       = aws_dynamodb_table.products.hash_key
}

output "app_role_arn" {
  description = "ARN of the IAM role the backend service assumes."
  value       = aws_iam_role.app.arn
}

output "app_role_name" {
  description = "Name of the IAM role the backend service assumes."
  value       = aws_iam_role.app.name
}

output "app_policy_arn" {
  description = "ARN of the least-privilege policy attached to the application role."
  value       = aws_iam_policy.app.arn
}

output "application_log_group_name" {
  description = "CloudWatch Logs group name the application logging handler targets."
  value       = aws_cloudwatch_log_group.application.name
}

output "application_log_group_arn" {
  description = "ARN of the application CloudWatch Logs group."
  value       = aws_cloudwatch_log_group.application.arn
}

output "kms_key_arn" {
  description = "ARN of the customer managed key encrypting the products table and application logs."
  value       = aws_kms_key.inventory.arn
}

output "aws_region" {
  description = "Region the inventory API resources are deployed to."
  value       = var.aws_region
}
