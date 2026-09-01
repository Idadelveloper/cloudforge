variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default: us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a default tag and name prefix."
  type        = string
  default     = "file_sharing_backend"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "s3_bucket_name" {
  description = "Name of the S3 bucket that stores uploaded file objects (plan: file-sharing-objects)."
  type        = string
  default     = "file-sharing-objects"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table holding file metadata (plan: file-metadata)."
  type        = string
  default     = "file-metadata"
}

variable "dynamodb_owner_index_name" {
  description = "Name of the global secondary index used for per-owner listing and usage aggregation."
  type        = string
  default     = "owner-index"
}

variable "iam_role_name" {
  description = "Name of the IAM role assumed by the backend service (plan: file-sharing-app-role)."
  type        = string
  default     = "file-sharing-app-role"
}

variable "log_group_name" {
  description = "CloudWatch Logs group the application writes operational logs to."
  type        = string
  default     = "/cloudforge/file-sharing-backend"
}

variable "log_retention_in_days" {
  description = "Retention period, in days, for the application CloudWatch log group."
  type        = number
  default     = 365
}

variable "noncurrent_version_expiration_days" {
  description = "Days after which noncurrent object versions in the objects bucket are expired."
  type        = number
  default     = 90
}

variable "access_log_expiration_days" {
  description = "Days after which S3 access log objects are expired."
  type        = number
  default     = 365
}

variable "s3_use_path_style" {
  description = "Use path-style S3 addressing (required by LocalStack)."
  type        = bool
  default     = true
}
