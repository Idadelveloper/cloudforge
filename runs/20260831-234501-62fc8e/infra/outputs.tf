output "media_bucket_name" {
  description = "S3 bucket holding image bytes; source of presigned PUT/GET URLs."
  value       = aws_s3_bucket.media.bucket
}

output "media_bucket_arn" {
  description = "ARN of the media bucket."
  value       = aws_s3_bucket.media.arn
}

output "media_access_logs_bucket_name" {
  description = "Bucket receiving server access logs for the media bucket."
  value       = aws_s3_bucket.logs.bucket
}

output "s3_object_prefix" {
  description = "Key prefix used for album image objects."
  value       = var.s3_object_prefix
}

output "albums_table_name" {
  description = "DynamoDB table for album metadata."
  value       = aws_dynamodb_table.albums.name
}

output "albums_table_arn" {
  description = "ARN of the albums table."
  value       = aws_dynamodb_table.albums.arn
}

output "images_table_name" {
  description = "DynamoDB table for image metadata (album_id / image_id)."
  value       = aws_dynamodb_table.images.name
}

output "images_table_arn" {
  description = "ARN of the images table."
  value       = aws_dynamodb_table.images.arn
}

output "app_role_name" {
  description = "Name of the application IAM role."
  value       = aws_iam_role.app.name
}

output "app_role_arn" {
  description = "ARN of the application IAM role that signs presigned URLs."
  value       = aws_iam_role.app.arn
}

output "log_group_name" {
  description = "CloudWatch Logs group for application logs."
  value       = aws_cloudwatch_log_group.app.name
}

output "app_config_secret_name" {
  description = "Secrets Manager secret name with runtime configuration."
  value       = aws_secretsmanager_secret.app_config.name
}

output "app_config_secret_arn" {
  description = "ARN of the runtime configuration secret."
  value       = aws_secretsmanager_secret.app_config.arn
}

output "kms_key_arn" {
  description = "Customer-managed KMS key encrypting S3 objects, DynamoDB tables, logs and the secret."
  value       = aws_kms_key.gallery.arn
}

output "presigned_url_ttl_seconds" {
  description = "TTL applied to presigned upload/download URLs."
  value       = var.presigned_url_ttl_seconds
}
