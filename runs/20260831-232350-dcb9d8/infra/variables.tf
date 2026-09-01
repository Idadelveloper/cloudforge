variable "aws_region" {
  description = "AWS region used for all image-gallery resources."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a default tag on every resource."
  type        = string
  default     = "image-gallery-backend"
}

variable "environment" {
  description = "Deployment environment name (used in tags and naming)."
  type        = string
  default     = "dev"
}

variable "media_bucket_name" {
  description = "Name of the S3 bucket that stores image binaries (plan: image-gallery-media)."
  type        = string
  default     = "image-gallery-media"
}

variable "log_bucket_name" {
  description = "Name of the S3 bucket that receives server access logs for the media bucket."
  type        = string
  default     = "image-gallery-media-access-logs"
}

variable "albums_table_name" {
  description = "DynamoDB table holding album metadata (plan: albums)."
  type        = string
  default     = "albums"
}

variable "images_table_name" {
  description = "DynamoDB table holding per-image metadata (plan: images)."
  type        = string
  default     = "images"
}

variable "app_policy_name" {
  description = "Name of the least-privilege IAM policy for the backend (plan: image-gallery-app-policy)."
  type        = string
  default     = "image-gallery-app-policy"
}

variable "app_role_name" {
  description = "Name of the IAM role (service access identity) the backend assumes."
  type        = string
  default     = "image-gallery-app-role"
}

variable "log_group_name" {
  description = "CloudWatch Logs group for backend application logs (plan: /image-gallery/app-logs)."
  type        = string
  default     = "/image-gallery/app-logs"
}

variable "log_retention_in_days" {
  description = "Retention period for the application log group."
  type        = number
  default     = 365
}

variable "cors_allowed_origins" {
  description = "Origins allowed to PUT/GET directly against presigned URLs from a browser."
  type        = list(string)
  default     = ["*"]
}

variable "noncurrent_version_expiration_days" {
  description = "Days after which non-current S3 object versions are expired."
  type        = number
  default     = 30
}

variable "abort_incomplete_multipart_days" {
  description = "Days after which incomplete multipart uploads are aborted."
  type        = number
  default     = 7
}
