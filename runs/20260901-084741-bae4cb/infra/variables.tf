variable "aws_region" {
  description = "AWS region used for all document-store resources."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a default tag to every resource."
  type        = string
  default     = "document_store"
}

variable "managed_by" {
  description = "Value for the managed-by default tag."
  type        = string
  default     = "cloudforge-terraform"
}

variable "documents_bucket_name" {
  description = "Name of the versioned S3 bucket holding uploaded document binaries."
  type        = string
  default     = "document-store-documents"
}

variable "access_logs_bucket_name" {
  description = "Name of the S3 bucket that receives server access logs for the documents bucket."
  type        = string
  default     = "document-store-documents-access-logs"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table indexing document version metadata."
  type        = string
  default     = "documents-metadata"
}

variable "tag_index_name" {
  description = "Name of the DynamoDB GSI used for tag search (tag / created_at)."
  type        = string
  default     = "tag-created_at-index"
}

variable "author_index_name" {
  description = "Name of the DynamoDB GSI used for listing documents by author (author / created_at)."
  type        = string
  default     = "author-created_at-index"
}

variable "service_role_name" {
  description = "Name of the IAM role assumed by the document-store backend service."
  type        = string
  default     = "document-store-service-role"
}

variable "service_policy_name" {
  description = "Name of the least-privilege IAM policy attached to the service role."
  type        = string
  default     = "document-store-service-policy"
}

variable "log_group_name" {
  description = "CloudWatch Logs group the application writes operational logs to."
  type        = string
  default     = "/cloudforge/document-store"
}

variable "log_retention_days" {
  description = "Retention period, in days, for the application CloudWatch log group."
  type        = number
  default     = 365
}

variable "app_config_secret_name" {
  description = "Secrets Manager secret holding the application configuration (API key, presigned URL settings)."
  type        = string
  default     = "document-store/app-config"
}

variable "presigned_url_default_expiry_seconds" {
  description = "Default expiry, in seconds, for generated presigned download URLs."
  type        = number
  default     = 900
}

variable "presigned_url_max_expiry_seconds" {
  description = "Maximum expiry, in seconds, allowed for generated presigned download URLs."
  type        = number
  default     = 3600
}

variable "max_upload_size_bytes" {
  description = "Maximum accepted upload size in bytes (10 MB by default)."
  type        = number
  default     = 10485760
}

variable "noncurrent_version_retention_days" {
  description = "Number of days non-current document object versions are retained before expiry."
  type        = number
  default     = 365
}

variable "access_log_retention_days" {
  description = "Number of days S3 server access logs are retained."
  type        = number
  default     = 90
}
