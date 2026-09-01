output "aws_region" {
  description = "Region the resources were created in."
  value       = var.aws_region
}

output "s3_bucket_name" {
  description = "Name of the S3 bucket that stores uploaded file objects."
  value       = aws_s3_bucket.objects.id
}

output "s3_bucket_arn" {
  description = "ARN of the S3 bucket that stores uploaded file objects."
  value       = aws_s3_bucket.objects.arn
}

output "s3_access_logs_bucket_name" {
  description = "Name of the S3 bucket receiving server access logs."
  value       = aws_s3_bucket.access_logs.id
}

output "dynamodb_table_name" {
  description = "Name of the DynamoDB table holding file metadata."
  value       = aws_dynamodb_table.file_metadata.name
}

output "dynamodb_table_arn" {
  description = "ARN of the DynamoDB file metadata table."
  value       = aws_dynamodb_table.file_metadata.arn
}

output "dynamodb_owner_index_name" {
  description = "Name of the GSI used for per-owner listing and usage aggregation."
  value       = var.dynamodb_owner_index_name
}

output "iam_role_name" {
  description = "Name of the IAM role used by the backend service."
  value       = aws_iam_role.app.name
}

output "iam_role_arn" {
  description = "ARN of the IAM role used by the backend service."
  value       = aws_iam_role.app.arn
}

output "iam_policy_arn" {
  description = "ARN of the least-privilege policy attached to the backend role."
  value       = aws_iam_policy.app.arn
}

output "cloudwatch_log_group_name" {
  description = "Name of the CloudWatch log group for application logs."
  value       = aws_cloudwatch_log_group.app.name
}

output "cloudwatch_log_group_arn" {
  description = "ARN of the CloudWatch log group for application logs."
  value       = aws_cloudwatch_log_group.app.arn
}
