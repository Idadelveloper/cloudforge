output "dynamodb_table_name" {
  description = "Name of the DynamoDB table holding the short code mappings; export as the app's table env var."
  value       = aws_dynamodb_table.url_shortener_mappings.name
}

output "dynamodb_table_arn" {
  description = "ARN of the URL shortener mappings table."
  value       = aws_dynamodb_table.url_shortener_mappings.arn
}

output "dynamodb_hash_key" {
  description = "Partition key attribute name of the mappings table."
  value       = aws_dynamodb_table.url_shortener_mappings.hash_key
}

output "service_role_name" {
  description = "Name of the IAM role assumed by the URL shortener service."
  value       = aws_iam_role.url_shortener_service_role.name
}

output "service_role_arn" {
  description = "ARN of the IAM role assumed by the URL shortener service."
  value       = aws_iam_role.url_shortener_service_role.arn
}

output "dynamodb_policy_arn" {
  description = "ARN of the least-privilege DynamoDB access policy attached to the service role."
  value       = aws_iam_policy.url_shortener_dynamodb_policy.arn
}

output "log_group_name" {
  description = "CloudWatch log group name for application logs."
  value       = aws_cloudwatch_log_group.url_shortener.name
}

output "log_group_arn" {
  description = "CloudWatch log group ARN for application logs."
  value       = aws_cloudwatch_log_group.url_shortener.arn
}

output "kms_key_arn" {
  description = "ARN of the customer managed KMS key encrypting table items and log data."
  value       = aws_kms_key.url_shortener.arn
}

output "aws_region" {
  description = "Region the URL shortener infrastructure is deployed in."
  value       = var.aws_region
}
