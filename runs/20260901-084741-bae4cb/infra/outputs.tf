output "aws_region" {
  description = "Region the document-store resources are deployed in."
  value       = local.region
}

output "documents_bucket_name" {
  description = "Name of the versioned S3 bucket storing document binaries."
  value       = aws_s3_bucket.documents.bucket
}

output "documents_bucket_arn" {
  description = "ARN of the versioned S3 bucket storing document binaries."
  value       = aws_s3_bucket.documents.arn
}

output "access_logs_bucket_name" {
  description = "Name of the S3 bucket receiving server access logs."
  value       = aws_s3_bucket.access_logs.bucket
}

output "metadata_table_name" {
  description = "Name of the DynamoDB table holding document version metadata."
  value       = aws_dynamodb_table.documents_metadata.name
}

output "metadata_table_arn" {
  description = "ARN of the DynamoDB metadata table."
  value       = aws_dynamodb_table.documents_metadata.arn
}

output "tag_index_name" {
  description = "Name of the DynamoDB GSI used for tag search."
  value       = var.tag_index_name
}

output "author_index_name" {
  description = "Name of the DynamoDB GSI used for author listing."
  value       = var.author_index_name
}

output "service_role_arn" {
  description = "ARN of the IAM role assumed by the document-store backend service."
  value       = aws_iam_role.service.arn
}

output "service_policy_arn" {
  description = "ARN of the least-privilege IAM policy for the service role."
  value       = aws_iam_policy.service.arn
}

output "log_group_name" {
  description = "CloudWatch Logs group used by the application."
  value       = aws_cloudwatch_log_group.app.name
}

output "app_config_secret_name" {
  description = "Name of the Secrets Manager secret holding the application configuration."
  value       = aws_secretsmanager_secret.app_config.name
}

output "app_config_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the application configuration."
  value       = aws_secretsmanager_secret.app_config.arn
}

output "presigned_url_default_expiry_seconds" {
  description = "Default presigned download URL expiry configured for the service."
  value       = var.presigned_url_default_expiry_seconds
}
