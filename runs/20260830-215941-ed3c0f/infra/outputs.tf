output "aws_region" {
  description = "Region the notes API resources were created in."
  value       = var.aws_region
}

output "notes_table_name" {
  description = "DynamoDB table name the application should use (NOTES_TABLE_NAME)."
  value       = aws_dynamodb_table.notes.name
}

output "notes_table_arn" {
  description = "ARN of the notes DynamoDB table."
  value       = aws_dynamodb_table.notes.arn
}

output "notes_table_hash_key" {
  description = "Partition key attribute of the notes table."
  value       = aws_dynamodb_table.notes.hash_key
}

output "app_log_group_name" {
  description = "CloudWatch Logs group name for application logs (LOG_GROUP_NAME)."
  value       = aws_cloudwatch_log_group.app.name
}

output "app_log_group_arn" {
  description = "ARN of the application CloudWatch log group."
  value       = aws_cloudwatch_log_group.app.arn
}

output "app_policy_arn" {
  description = "ARN of the least-privilege IAM policy for the notes API service."
  value       = aws_iam_policy.app.arn
}

output "app_role_arn" {
  description = "ARN of the IAM role the notes API service assumes."
  value       = aws_iam_role.app.arn
}

output "encryption_key_arn" {
  description = "ARN of the customer managed KMS key encrypting the table and log group."
  value       = aws_kms_key.notes.arn
}
