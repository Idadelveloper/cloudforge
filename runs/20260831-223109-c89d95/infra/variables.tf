variable "aws_region" {
  description = "AWS region used for all resources (LocalStack defaults to us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a default tag on every resource."
  type        = string
  default     = "file_share_backend"
}

variable "environment" {
  description = "Deployment environment name applied as a default tag."
  type        = string
  default     = "dev"
}

variable "files_bucket_name" {
  description = "Name of the S3 bucket that stores uploaded file objects."
  type        = string
  default     = "file-share-files"
}

variable "access_logs_bucket_name" {
  description = "Name of the S3 bucket that receives server access logs for the files bucket."
  type        = string
  default     = "file-share-files-logs"
}

variable "metadata_table_name" {
  description = "Name of the DynamoDB table holding file metadata records."
  type        = string
  default     = "file-share-metadata"
}

variable "owner_index_name" {
  description = "Name of the DynamoDB global secondary index used to list files and compute usage per owner."
  type        = string
  default     = "owner-index"
}

variable "app_role_name" {
  description = "Name of the IAM role assumed by the file-share backend service."
  type        = string
  default     = "file-share-app-role"
}

variable "app_policy_name" {
  description = "Name of the least-privilege IAM policy attached to the application role."
  type        = string
  default     = "file-share-app-policy"
}

variable "log_group_name" {
  description = "CloudWatch Logs group the application writes structured request and error logs to."
  type        = string
  default     = "/file-share/app-logs"
}

variable "log_retention_days" {
  description = "Retention period, in days, for the application CloudWatch log group."
  type        = number
  default     = 365
}

variable "cors_allowed_origins" {
  description = "Origins permitted to PUT/GET objects directly against the files bucket using presigned URLs."
  type        = list(string)
  default     = ["*"]
}

variable "noncurrent_version_retention_days" {
  description = "Number of days non-current object versions are retained before expiration."
  type        = number
  default     = 30
}

variable "abort_incomplete_multipart_days" {
  description = "Number of days after which incomplete multipart uploads are aborted."
  type        = number
  default     = 7
}
