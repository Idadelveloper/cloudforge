output "aws_region" {
  description = "Region the document-store resources were created in."
  value       = var.aws_region
}

output "documents_bucket_name" {
  description = "Name of the versioned S3 bucket holding document binaries."
  value       = aws_s3_bucket.documents.bucket
}

output "documents_bucket_arn" {
  description = "ARN of the documents S3 bucket."
  value       = aws_s3_bucket.documents.arn
}

output "access_logs_bucket_name" {
  description = "Name of the S3 bucket receiving server access logs for the documents bucket."
  value       = aws_s3_bucket.access_logs.bucket
}

output "metadata_table_name" {
  description = "DynamoDB table storing per-version document metadata."
  value       = aws_dynamodb_table.document_metadata.name
}

output "metadata_table_arn" {
  description = "ARN of the document metadata table."
  value       = aws_dynamodb_table.document_metadata.arn
}

output "tag_index_table_name" {
  description = "DynamoDB table backing tag search."
  value       = aws_dynamodb_table.document_tag_index.name
}

output "tag_index_table_arn" {
  description = "ARN of the tag index table."
  value       = aws_dynamodb_table.document_tag_index.arn
}

output "app_log_group_name" {
  description = "CloudWatch Logs group used by the backend."
  value       = aws_cloudwatch_log_group.app.name
}

output "app_role_name" {
  description = "Name of the application IAM role."
  value       = aws_iam_role.app.name
}

output "app_role_arn" {
  description = "ARN of the application IAM role."
  value       = aws_iam_role.app.arn
}

output "app_policy_arn" {
  description = "ARN of the least-privilege application IAM policy."
  value       = aws_iam_policy.app.arn
}

output "account_id" {
  description = "Account id the resources were deployed into."
  value       = data.aws_caller_identity.current.account_id
}
