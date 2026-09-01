variable "aws_region" {
  description = "AWS region used for all blog platform resources."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name applied as a tag on every resource."
  type        = string
  default     = "blog_platform_backend"
}

variable "managed_by" {
  description = "Value for the managed-by tag applied to every resource."
  type        = string
  default     = "cloudforge-terraform"
}

variable "posts_table_name" {
  description = "DynamoDB table holding blog post items (partition key post_id)."
  type        = string
  default     = "blog-posts"
}

variable "published_comments_table_name" {
  description = "DynamoDB table holding approved comments (post_id / comment_id)."
  type        = string
  default     = "blog-published-comments"
}

variable "images_bucket_name" {
  description = "S3 bucket holding uploaded post images."
  type        = string
  default     = "blog-post-images"
}

variable "logs_bucket_name" {
  description = "S3 bucket receiving server access logs for the images bucket."
  type        = string
  default     = "blog-post-images-access-logs"
}

variable "moderation_queue_name" {
  description = "SQS queue holding comments awaiting moderation."
  type        = string
  default     = "blog-comment-moderation-queue"
}

variable "moderation_dlq_name" {
  description = "SQS dead-letter queue for the comment moderation queue."
  type        = string
  default     = "blog-comment-moderation-dlq"
}

variable "moderation_queue_visibility_timeout" {
  description = "Seconds a received moderation message stays invisible while a moderator decides."
  type        = number
  default     = 30
}

variable "moderation_queue_retention_seconds" {
  description = "Seconds a pending comment message is retained on the moderation queue."
  type        = number
  default     = 345600
}

variable "moderation_dlq_retention_seconds" {
  description = "Seconds a failed comment message is retained on the dead-letter queue."
  type        = number
  default     = 1209600
}

variable "moderation_max_receive_count" {
  description = "Number of moderation delivery attempts before a message is moved to the DLQ."
  type        = number
  default     = 5
}

variable "app_policy_name" {
  description = "Name of the least-privilege IAM policy for the backend service."
  type        = string
  default     = "blog-backend-app-policy"
}

variable "app_role_name" {
  description = "Name of the IAM role the backend service assumes."
  type        = string
  default     = "blog-backend-app-role"
}

variable "log_group_name" {
  description = "CloudWatch log group for backend application and moderation-audit logs."
  type        = string
  default     = "/blog-platform/blog-backend-logs"
}

variable "log_retention_in_days" {
  description = "Retention period for the backend CloudWatch log group."
  type        = number
  default     = 365
}

variable "images_noncurrent_version_expiration_days" {
  description = "Days after which noncurrent image versions are expired."
  type        = number
  default     = 30
}
