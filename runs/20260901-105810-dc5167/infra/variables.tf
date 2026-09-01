variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project tag applied to every resource via provider default_tags."
  type        = string
  default     = "blog-platform-backend"
}

variable "managed_by" {
  description = "managed-by tag applied to every resource via provider default_tags."
  type        = string
  default     = "terraform"
}

variable "posts_table_name" {
  description = "DynamoDB table holding post items (markdown body, metadata, image keys)."
  type        = string
  default     = "blog-posts"
}

variable "comments_table_name" {
  description = "DynamoDB table holding published (approved) comments."
  type        = string
  default     = "blog-comments"
}

variable "images_bucket_name" {
  description = "S3 bucket that stores uploaded post images."
  type        = string
  default     = "blog-post-images"
}

variable "images_log_bucket_name" {
  description = "S3 bucket that receives server access logs for the post images bucket."
  type        = string
  default     = "blog-post-images-logs"
}

variable "moderation_queue_name" {
  description = "SQS queue holding submitted comments awaiting moderation."
  type        = string
  default     = "blog-comment-moderation"
}

variable "moderation_dlq_name" {
  description = "SQS dead-letter queue for the comment moderation queue."
  type        = string
  default     = "blog-comment-moderation-dlq"
}

variable "service_role_name" {
  description = "IAM role assumed by the standalone backend service."
  type        = string
  default     = "blog-backend-service-role"
}

variable "log_group_name" {
  description = "CloudWatch log group for backend application and moderation-decision logs."
  type        = string
  default     = "blog-backend-logs"
}

variable "log_retention_days" {
  description = "Retention (days) for the backend CloudWatch log group."
  type        = number
  default     = 365
}

variable "moderation_visibility_timeout_seconds" {
  description = "Visibility timeout for received moderation messages, giving moderators time to approve or reject."
  type        = number
  default     = 300
}

variable "moderation_message_retention_seconds" {
  description = "How long an unmoderated comment stays on the moderation queue."
  type        = number
  default     = 1209600
}

variable "moderation_max_receive_count" {
  description = "Receives of a moderation message before it is redriven to the DLQ."
  type        = number
  default     = 5
}

variable "moderation_backlog_alarm_threshold" {
  description = "Number of visible moderation messages that triggers the backlog alarm."
  type        = number
  default     = 50
}

variable "image_noncurrent_version_expiration_days" {
  description = "Days after which non-current image object versions are expired."
  type        = number
  default     = 30
}

variable "log_object_expiration_days" {
  description = "Days after which S3 access log objects expire."
  type        = number
  default     = 90
}
