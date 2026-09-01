output "messages_table_name" {
  description = "Name of the DynamoDB table storing contact messages."
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
  description = "Secrets Manager secret name holding the admin API key."
  value       = aws_secretsmanager_secret.admin_api_key.name
}

output "admin_api_key_secret_arn" {
  description = "ARN of the admin API key secret."
  value       = aws_secretsmanager_secret.admin_api_key.arn
}

output "log_group_name" {
  description = "CloudWatch Logs group used by the application."
  value       = aws_cloudwatch_log_group.backend.name
}

output "log_group_arn" {
  description = "ARN of the application CloudWatch log group."
  value       = aws_cloudwatch_log_group.backend.arn
}

output "app_role_name" {
  description = "IAM role name for the backend service."
  value       = aws_iam_role.app.name
}

output "app_role_arn" {
  description = "IAM role ARN for the backend service."
  value       = aws_iam_role.app.arn
}

output "kms_key_arn" {
  description = "ARN of the customer managed key protecting the table, secret and logs."
  value       = aws_kms_key.contact_form.arn
}

output "aws_region" {
  description = "Region the resources were created in."
  value       = var.aws_region
}
