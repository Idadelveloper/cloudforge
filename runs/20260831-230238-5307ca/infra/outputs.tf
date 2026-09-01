output "aws_region" {
  description = "Region the stack is deployed into."
  value       = var.aws_region
}

output "bookmarks_table_name" {
  description = "Name of the DynamoDB table storing bookmarks (BOOKMARKS_TABLE env var)."
  value       = aws_dynamodb_table.bookmarks.name
}

output "bookmarks_table_arn" {
  description = "ARN of the DynamoDB bookmarks table."
  value       = aws_dynamodb_table.bookmarks.arn
}

output "bookmarks_tag_index_name" {
  description = "Name of the tag global secondary index (BOOKMARKS_TAG_INDEX env var)."
  value       = var.dynamodb_tag_index_name
}

output "api_key_secret_name" {
  description = "Secrets Manager secret name holding the shared API key (API_KEY_SECRET_NAME env var)."
  value       = aws_secretsmanager_secret.api_key.name
}

output "api_key_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the shared API key."
  value       = aws_secretsmanager_secret.api_key.arn
}

output "api_key" {
  description = "Shared API key value expected in the X-API-Key header."
  value       = var.api_key
  sensitive   = true
}

output "log_group_name" {
  description = "CloudWatch Logs group for application logs (LOG_GROUP_NAME env var)."
  value       = aws_cloudwatch_log_group.app.name
}

output "app_role_arn" {
  description = "ARN of the IAM role used by the bookmark manager service."
  value       = aws_iam_role.app.arn
}

output "app_policy_arn" {
  description = "ARN of the least-privilege IAM policy attached to the service role."
  value       = aws_iam_policy.app.arn
}
