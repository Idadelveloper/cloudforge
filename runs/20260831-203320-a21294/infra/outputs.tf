output "aws_region" {
  description = "Region the resources were created in."
  value       = var.aws_region
}

output "messages_table_name" {
  description = "Name of the DynamoDB table holding contact messages (TABLE_NAME env var for the app)."
  value       = aws_dynamodb_table.contact_messages.name
}

output "messages_table_arn" {
  description = "ARN of the contact messages DynamoDB table."
  value       = aws_dynamodb_table.contact_messages.arn
}

output "messages_table_hash_key" {
  description = "Partition key attribute of the contact messages table."
  value       = aws_dynamodb_table.contact_messages.hash_key
}

output "app_role_name" {
  description = "Name of the IAM role assumed by the backend service."
  value       = aws_iam_role.app.name
}

output "app_role_arn" {
  description = "ARN of the IAM role assumed by the backend service."
  value       = aws_iam_role.app.arn
}

output "app_policy_arn" {
  description = "ARN of the least-privilege policy attached to the application role."
  value       = aws_iam_policy.app.arn
}

output "log_group_name" {
  description = "CloudWatch log group name the application writes logs to."
  value       = aws_cloudwatch_log_group.app.name
}

output "log_group_arn" {
  description = "CloudWatch log group ARN."
  value       = aws_cloudwatch_log_group.app.arn
}

output "kms_key_arn" {
  description = "ARN of the customer managed key encrypting the table and log group."
  value       = aws_kms_key.contact_form.arn
}
