output "notes_table_name" {
  description = "Name of the DynamoDB notes table; inject as NOTES_TABLE_NAME into the application."
  value       = aws_dynamodb_table.notes.name
}

output "notes_table_arn" {
  description = "ARN of the DynamoDB notes table."
  value       = aws_dynamodb_table.notes.arn
}

output "notes_table_hash_key" {
  description = "Partition key attribute name of the notes table."
  value       = aws_dynamodb_table.notes.hash_key
}

output "app_role_name" {
  description = "Name of the least-privilege IAM role used by the notes API."
  value       = aws_iam_role.app.name
}

output "app_role_arn" {
  description = "ARN of the least-privilege IAM role used by the notes API."
  value       = aws_iam_role.app.arn
}

output "app_policy_arn" {
  description = "ARN of the managed policy attached to the notes API role."
  value       = aws_iam_policy.app.arn
}

output "log_group_name" {
  description = "CloudWatch log group the notes API writes request and error logs to."
  value       = aws_cloudwatch_log_group.notes_api.name
}

output "log_group_arn" {
  description = "ARN of the notes API CloudWatch log group."
  value       = aws_cloudwatch_log_group.notes_api.arn
}

output "kms_key_arn" {
  description = "ARN of the customer managed KMS key encrypting the notes table and log group."
  value       = aws_kms_key.notes.arn
}

output "aws_region" {
  description = "Region the notes API infrastructure is deployed to."
  value       = var.aws_region
}
