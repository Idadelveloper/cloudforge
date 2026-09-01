output "messages_table_name" {
  description = "Name of the DynamoDB table storing contact-form messages."
  value       = aws_dynamodb_table.messages.name
}

output "messages_table_arn" {
  description = "ARN of the DynamoDB messages table."
  value       = aws_dynamodb_table.messages.arn
}

output "messages_table_hash_key" {
  description = "Partition key attribute of the messages table."
  value       = aws_dynamodb_table.messages.hash_key
}

output "admin_api_key_secret_name" {
  description = "Secrets Manager secret name holding the administrator API key."
  value       = aws_secretsmanager_secret.admin_api_key.name
}

output "admin_api_key_secret_arn" {
  description = "ARN of the administrator API key secret."
  value       = aws_secretsmanager_secret.admin_api_key.arn
}

output "app_log_group_name" {
  description = "CloudWatch Logs group used by the FastAPI service."
  value       = aws_cloudwatch_log_group.app.name
}

output "app_log_group_arn" {
  description = "ARN of the application CloudWatch Logs group."
  value       = aws_cloudwatch_log_group.app.arn
}

output "app_role_name" {
  description = "Name of the IAM role assumed by the contact-form backend."
  value       = aws_iam_role.app.name
}

output "app_role_arn" {
  description = "ARN of the IAM role assumed by the contact-form backend."
  value       = aws_iam_role.app.arn
}

output "kms_key_arn" {
  description = "ARN of the customer managed key encrypting DynamoDB, Secrets Manager and CloudWatch Logs data."
  value       = aws_kms_key.this.arn
}

output "aws_region" {
  description = "Region the contact-form backend resources are deployed to."
  value       = var.aws_region
}
