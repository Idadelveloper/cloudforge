output "dynamodb_table_name" {
  description = "Name of the DynamoDB table holding short-code mappings (set as the app's table env var)."
  value       = aws_dynamodb_table.urls.name
}

output "dynamodb_table_arn" {
  description = "ARN of the short URL DynamoDB table."
  value       = aws_dynamodb_table.urls.arn
}

output "dynamodb_hash_key" {
  description = "Partition key attribute name of the short URL table."
  value       = aws_dynamodb_table.urls.hash_key
}

output "app_role_arn" {
  description = "ARN of the IAM role the url_shortener backend assumes."
  value       = aws_iam_role.app.arn
}

output "app_policy_arn" {
  description = "ARN of the least-privilege policy attached to the application role."
  value       = aws_iam_policy.app.arn
}

output "log_group_name" {
  description = "CloudWatch Logs group name for application logs."
  value       = aws_cloudwatch_log_group.app.name
}

output "log_group_arn" {
  description = "ARN of the application CloudWatch log group."
  value       = aws_cloudwatch_log_group.app.arn
}

output "kms_key_arn" {
  description = "ARN of the CMK encrypting the DynamoDB table and log group."
  value       = aws_kms_key.url_shortener.arn
}

output "aws_region" {
  description = "Region the url_shortener resources are deployed in."
  value       = var.aws_region
}
