output "media_bucket_name" {
  description = "S3 bucket holding image binaries; set as the backend's media bucket env var."
  value       = aws_s3_bucket.media.id
}

output "media_bucket_arn" {
  description = "ARN of the media bucket."
  value       = aws_s3_bucket.media.arn
}

output "media_bucket_regional_domain_name" {
  description = "Regional domain name of the media bucket (useful for presigned URL hosts)."
  value       = aws_s3_bucket.media.bucket_regional_domain_name
}

output "access_log_bucket_name" {
  description = "Bucket receiving S3 server access logs for the media bucket."
  value       = aws_s3_bucket.logs.id
}

output "albums_table_name" {
  description = "DynamoDB table name for album metadata."
  value       = aws_dynamodb_table.albums.name
}

output "albums_table_arn" {
  description = "ARN of the albums table."
  value       = aws_dynamodb_table.albums.arn
}

output "images_table_name" {
  description = "DynamoDB table name for image metadata."
  value       = aws_dynamodb_table.images.name
}

output "images_table_arn" {
  description = "ARN of the images table."
  value       = aws_dynamodb_table.images.arn
}

output "app_log_group_name" {
  description = "CloudWatch Logs group the backend writes structured logs to."
  value       = aws_cloudwatch_log_group.app.name
}

output "app_role_arn" {
  description = "IAM role (access identity) for the backend process."
  value       = aws_iam_role.app.arn
}

output "app_policy_arn" {
  description = "ARN of the least-privilege policy attached to the backend role."
  value       = aws_iam_policy.app.arn
}

output "kms_key_arn" {
  description = "CMK encrypting S3 objects, DynamoDB tables and application logs."
  value       = aws_kms_key.gallery.arn
}
