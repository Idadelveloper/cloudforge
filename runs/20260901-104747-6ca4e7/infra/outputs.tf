output "aws_region" {
  description = "Region the blog platform resources were created in."
  value       = var.aws_region
}

output "posts_table_name" {
  description = "Name of the DynamoDB table storing blog posts."
  value       = aws_dynamodb_table.posts.name
}

output "posts_table_arn" {
  description = "ARN of the DynamoDB table storing blog posts."
  value       = aws_dynamodb_table.posts.arn
}

output "published_comments_table_name" {
  description = "Name of the DynamoDB table storing approved comments."
  value       = aws_dynamodb_table.published_comments.name
}

output "published_comments_table_arn" {
  description = "ARN of the DynamoDB table storing approved comments."
  value       = aws_dynamodb_table.published_comments.arn
}

output "images_bucket_name" {
  description = "Name of the S3 bucket holding post images."
  value       = aws_s3_bucket.images.bucket
}

output "images_bucket_arn" {
  description = "ARN of the S3 bucket holding post images."
  value       = aws_s3_bucket.images.arn
}

output "images_access_logs_bucket_name" {
  description = "Name of the S3 bucket receiving access logs for the images bucket."
  value       = aws_s3_bucket.logs.bucket
}

output "moderation_queue_name" {
  description = "Name of the SQS comment moderation queue."
  value       = aws_sqs_queue.moderation.name
}

output "moderation_queue_url" {
  description = "URL of the SQS comment moderation queue used by the backend."
  value       = aws_sqs_queue.moderation.url
}

output "moderation_queue_arn" {
  description = "ARN of the SQS comment moderation queue."
  value       = aws_sqs_queue.moderation.arn
}

output "moderation_dlq_url" {
  description = "URL of the SQS comment moderation dead-letter queue."
  value       = aws_sqs_queue.moderation_dlq.url
}

output "moderation_dlq_arn" {
  description = "ARN of the SQS comment moderation dead-letter queue."
  value       = aws_sqs_queue.moderation_dlq.arn
}

output "log_group_name" {
  description = "CloudWatch log group the backend writes application and moderation-audit logs to."
  value       = aws_cloudwatch_log_group.backend.name
}

output "app_role_arn" {
  description = "ARN of the IAM role the backend service assumes."
  value       = aws_iam_role.app.arn
}

output "app_policy_arn" {
  description = "ARN of the least-privilege IAM policy attached to the backend role."
  value       = aws_iam_policy.app.arn
}
