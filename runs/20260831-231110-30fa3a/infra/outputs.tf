output "aws_region" {
  description = "Region the resources were created in."
  value       = var.aws_region
}

output "bookmarks_table_name" {
  description = "Name of the primary bookmarks DynamoDB table."
  value       = aws_dynamodb_table.bookmarks.name
}

output "bookmarks_table_arn" {
  description = "ARN of the primary bookmarks DynamoDB table."
  value       = aws_dynamodb_table.bookmarks.arn
}

output "bookmark_tags_table_name" {
  description = "Name of the bookmark/tag lookup DynamoDB table."
  value       = aws_dynamodb_table.bookmark_tags.name
}

output "bookmark_tags_table_arn" {
  description = "ARN of the bookmark/tag lookup DynamoDB table."
  value       = aws_dynamodb_table.bookmark_tags.arn
}

output "bookmark_tags_index_name" {
  description = "GSI used for newest-first listings within a tag."
  value       = var.tag_created_at_index_name
}

output "api_key_secret_name" {
  description = "Secrets Manager secret name holding the shared API key."
  value       = aws_secretsmanager_secret.api_key.name
}

output "api_key_secret_arn" {
  description = "Secrets Manager secret ARN holding the shared API key."
  value       = aws_secretsmanager_secret.api_key.arn
}

output "app_role_name" {
  description = "IAM role name for the bookmark manager service."
  value       = aws_iam_role.app.name
}

output "app_role_arn" {
  description = "IAM role ARN for the bookmark manager service."
  value       = aws_iam_role.app.arn
}

output "app_policy_arn" {
  description = "ARN of the least privilege policy attached to the service role."
  value       = aws_iam_policy.app.arn
}

output "log_group_name" {
  description = "CloudWatch Logs group for application logs."
  value       = aws_cloudwatch_log_group.app.name
}

output "kms_key_arn" {
  description = "ARN of the customer managed key protecting tables, secret and logs."
  value       = aws_kms_key.main.arn
}

output "app_environment" {
  description = "Environment variables the application should be started with."
  value = {
    AWS_REGION               = var.aws_region
    BOOKMARKS_TABLE_NAME     = aws_dynamodb_table.bookmarks.name
    BOOKMARK_TAGS_TABLE_NAME = aws_dynamodb_table.bookmark_tags.name
    BOOKMARK_TAGS_INDEX_NAME = var.tag_created_at_index_name
    API_KEY_SECRET_NAME      = aws_secretsmanager_secret.api_key.name
    LOG_GROUP_NAME           = aws_cloudwatch_log_group.app.name
  }
}
