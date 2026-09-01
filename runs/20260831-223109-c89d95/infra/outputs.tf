output "aws_region" {
  description = "Region the file-share resources are deployed to."
  value       = var.aws_region
}

output "files_bucket_name" {
  description = "Name of the S3 bucket that stores uploaded files (set as S3_BUCKET for the app)."
  value       = aws_s3_bucket.files.id
}

output "files_bucket_arn" {
  description = "ARN of the S3 bucket that stores uploaded files."
  value       = aws_s3_bucket.files.arn
}

output "files_bucket_regional_domain_name" {
  description = "Regional domain name of the files bucket, useful for presigned URL construction."
  value       = aws_s3_bucket.files.bucket_regional_domain_name
}

output "access_logs_bucket_name" {
  description = "Name of the S3 bucket receiving server access logs for the files bucket."
  value       = aws_s3_bucket.access_logs.id
}

output "metadata_table_name" {
  description = "Name of the DynamoDB table holding file metadata (set as DYNAMODB_TABLE for the app)."
  value       = aws_dynamodb_table.metadata.name
}

output "metadata_table_arn" {
  description = "ARN of the DynamoDB metadata table."
  value       = aws_dynamodb_table.metadata.arn
}

output "owner_index_name" {
  description = "Name of the DynamoDB GSI used for per-owner listing and usage aggregation."
  value       = var.owner_index_name
}

output "app_role_name" {
  description = "Name of the IAM role used by the file-share backend service."
  value       = aws_iam_role.app.name
}

output "app_role_arn" {
  description = "ARN of the IAM role used by the file-share backend service."
  value       = aws_iam_role.app.arn
}

output "app_policy_arn" {
  description = "ARN of the least-privilege IAM policy attached to the application role."
  value       = aws_iam_policy.app.arn
}

output "log_group_name" {
  description = "CloudWatch log group the application writes structured logs to."
  value       = aws_cloudwatch_log_group.app.name
}

output "log_group_arn" {
  description = "ARN of the application CloudWatch log group."
  value       = aws_cloudwatch_log_group.app.arn
}
