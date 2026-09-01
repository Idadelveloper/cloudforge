output "aws_region" {
  description = "Region the blog platform resources were created in."
  value       = data.aws_region.current.name
}

output "account_id" {
  description = "Account id owning the blog platform resources."
  value       = data.aws_caller_identity.current.account_id
}

output "posts_table_name" {
  description = "Name of the DynamoDB posts table."
  value       = aws_dynamodb_table.posts.name
}

output "posts_table_arn" {
  description = "ARN of the DynamoDB posts table."
  value       = aws_dynamodb_table.posts.arn
}

output "comments_table_name" {
  description = "Name of the DynamoDB published comments table."
  value       = aws_dynamodb_table.comments.name
}

output "comments_table_arn" {
  description = "ARN of the DynamoDB published comments table."
  value       = aws_dynamodb_table.comments.arn
}

output "images_bucket_name" {
  description = "Name of the S3 bucket storing post images."
  value       = aws_s3_bucket.post_images.bucket
}

output "images_bucket_arn" {
  description = "ARN of the S3 bucket storing post images."
  value       = aws_s3_bucket.post_images.arn
}

output "images_log_bucket_name" {
  description = "Name of the S3 access log bucket for post images."
  value       = aws_s3_bucket.images_logs.bucket
}

output "moderation_queue_name" {
  description = "Name of the comment moderation queue."
  value       = aws_sqs_queue.comment_moderation.name
}

output "moderation_queue_url" {
  description = "URL of the comment moderation queue (used by the backend for send/receive/delete)."
  value       = aws_sqs_queue.comment_moderation.id
}

output "moderation_queue_arn" {
  description = "ARN of the comment moderation queue."
  value       = aws_sqs_queue.comment_moderation.arn
}

output "moderation_dlq_url" {
  description = "URL of the comment moderation dead-letter queue."
  value       = aws_sqs_queue.comment_moderation_dlq.id
}

output "moderation_dlq_arn" {
  description = "ARN of the comment moderation dead-letter queue."
  value       = aws_sqs_queue.comment_moderation_dlq.arn
}

output "backend_role_name" {
  description = "Name of the IAM role for the backend service."
  value       = aws_iam_role.backend.name
}

output "backend_role_arn" {
  description = "ARN of the IAM role for the backend service."
  value       = aws_iam_role.backend.arn
}

output "backend_policy_arn" {
  description = "ARN of the least-privilege policy attached to the backend role."
  value       = aws_iam_policy.backend.arn
}

output "log_group_name" {
  description = "CloudWatch log group used for backend and moderation-decision logs."
  value       = aws_cloudwatch_log_group.backend.name
}

output "moderation_backlog_alarm_name" {
  description = "CloudWatch alarm watching the moderation queue depth."
  value       = aws_cloudwatch_metric_alarm.moderation_backlog.alarm_name
}
