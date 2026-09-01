output "products_table_name" {
  description = "Name of the DynamoDB products table (set as the app's table environment variable)."
  value       = aws_dynamodb_table.products.name
}

output "products_table_arn" {
  description = "ARN of the DynamoDB products table."
  value       = aws_dynamodb_table.products.arn
}

output "products_sku_index_name" {
  description = "Name of the global secondary index used for SKU lookups and duplicate detection."
  value       = var.sku_index_name
}

output "application_log_group_name" {
  description = "CloudWatch Logs group name for application and stock-adjustment audit logs."
  value       = aws_cloudwatch_log_group.application.name
}

output "application_log_group_arn" {
  description = "ARN of the application CloudWatch Logs group."
  value       = aws_cloudwatch_log_group.application.arn
}

output "inventory_api_role_name" {
  description = "Name of the IAM role assumed by the inventory API service."
  value       = aws_iam_role.inventory_api.name
}

output "inventory_api_role_arn" {
  description = "ARN of the IAM role assumed by the inventory API service."
  value       = aws_iam_role.inventory_api.arn
}

output "kms_key_arn" {
  description = "ARN of the customer managed KMS key encrypting the table and log data."
  value       = aws_kms_key.inventory.arn
}

output "aws_region" {
  description = "Region the resources were created in."
  value       = var.aws_region
}
