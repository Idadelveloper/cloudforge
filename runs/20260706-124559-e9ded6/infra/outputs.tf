output "notes_table_name" {
  description = "Name of the DynamoDB notes table."
  value       = aws_dynamodb_table.notes_table.name
}

output "notes_table_arn" {
  description = "ARN of the DynamoDB notes table."
  value       = aws_dynamodb_table.notes_table.arn
}

output "notes_api_role_arn" {
  description = "ARN of the IAM execution role for the notes API."
  value       = aws_iam_role.notes_api_role.arn
}

output "notes_api_role_name" {
  description = "Name of the IAM execution role for the notes API."
  value       = aws_iam_role.notes_api_role.name
}

output "notes_api_log_group_name" {
  description = "Name of the CloudWatch log group for the notes API."
  value       = aws_cloudwatch_log_group.notes_api_logs.name
}
