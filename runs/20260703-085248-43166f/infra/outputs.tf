output "dynamodb_table_name" {
  description = "Name of the DynamoDB notes table."
  value       = aws_dynamodb_table.notes.name
}

output "dynamodb_table_arn" {
  description = "ARN of the DynamoDB notes table."
  value       = aws_dynamodb_table.notes.arn
}

output "iam_role_arn" {
  description = "ARN of the notes API execution role."
  value       = aws_iam_role.notes_api.arn
}

output "iam_role_name" {
  description = "Name of the notes API execution role."
  value       = aws_iam_role.notes_api.name
}

output "cloudwatch_log_group_name" {
  description = "Name of the CloudWatch log group for the notes API."
  value       = aws_cloudwatch_log_group.notes.name
}

output "kms_key_arn" {
  description = "ARN of the KMS key used for encryption."
  value       = aws_kms_key.notes.arn
}
