output "notes_table_name" {
  description = "DynamoDB table name to pass to the application as NOTES_TABLE_NAME."
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

output "application_log_group_name" {
  description = "CloudWatch Logs group the service writes structured logs to."
  value       = aws_cloudwatch_log_group.application.name
}

output "application_log_group_arn" {
  description = "ARN of the application CloudWatch Logs group."
  value       = aws_cloudwatch_log_group.application.arn
}

output "service_role_name" {
  description = "Name of the IAM role the notes API service assumes."
  value       = aws_iam_role.notes_api_service.name
}

output "service_role_arn" {
  description = "ARN of the IAM role the notes API service assumes."
  value       = aws_iam_role.notes_api_service.arn
}

output "service_policy_arn" {
  description = "ARN of the least-privilege policy attached to the service role."
  value       = aws_iam_policy.notes_api_service.arn
}

output "kms_key_arn" {
  description = "ARN of the customer managed key encrypting the table and logs."
  value       = aws_kms_key.notes.arn
}
