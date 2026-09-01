output "products_table_name" {
  description = "Name of the DynamoDB products table; export as PRODUCTS_TABLE for the FastAPI service."
  value       = aws_dynamodb_table.products.name
}

output "products_table_arn" {
  description = "ARN of the DynamoDB products table."
  value       = aws_dynamodb_table.products.arn
}

output "products_table_hash_key" {
  description = "Partition key attribute of the products table."
  value       = aws_dynamodb_table.products.hash_key
}

output "api_role_name" {
  description = "Name of the IAM role assumed by the inventory API service."
  value       = aws_iam_role.api.name
}

output "api_role_arn" {
  description = "ARN of the IAM role assumed by the inventory API service."
  value       = aws_iam_role.api.arn
}

output "dynamodb_access_policy_arn" {
  description = "ARN of the least-privilege policy attached to the inventory API role."
  value       = aws_iam_policy.dynamodb_access.arn
}

output "api_log_group_name" {
  description = "CloudWatch log group name for the inventory API."
  value       = aws_cloudwatch_log_group.api.name
}

output "api_log_group_arn" {
  description = "CloudWatch log group ARN for the inventory API."
  value       = aws_cloudwatch_log_group.api.arn
}

output "kms_key_arn" {
  description = "ARN of the customer managed KMS key encrypting the products table and API logs."
  value       = aws_kms_key.inventory.arn
}

output "aws_region" {
  description = "Region the inventory API resources are deployed to."
  value       = var.aws_region
}
