variable "aws_region" {
  description = "AWS region used for all document-store resources."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Logical project name, applied as a provider default tag."
  type        = string
  default     = "document_store"
}

variable "managed_by" {
  description = "Value for the managed-by default tag."
  type        = string
  default     = "cloudforge-terraform"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "local"
}

variable "documents_bucket_name" {
  description = "Name of the versioning-enabled S3 bucket that stores every uploaded document version."
  type        = string
  default     = "document-store-documents"
}

variable "access_logs_bucket_name" {
  description = "Name of the S3 bucket that receives server access logs for the documents bucket."
  type        = string
  default     = "document-store-documents-logs"
}

variable "metadata_table_name" {
  description = "DynamoDB table holding per-version document metadata (PK document_id, SK version)."
  type        = string
  default     = "document-metadata"
}

variable "tag_index_table_name" {
  description = "DynamoDB table backing tag search (PK tag, SK document_id)."
  type        = string
  default     = "document-tag-index"
}

variable "app_role_name" {
  description = "Name of the IAM role assumed by the document-store backend service."
  type        = string
  default     = "document-store-app-role"
}

variable "app_policy_name" {
  description = "Name of the least-privilege IAM policy attached to the application role."
  type        = string
  default     = "document-store-app-policy"
}

variable "log_group_name" {
  description = "CloudWatch Logs group the backend writes structured request and audit logs to."
  type        = string
  default     = "/document-store/app"
}

variable "log_retention_in_days" {
  description = "Retention period, in days, for the application CloudWatch log group."
  type        = number
  default     = 90
}

variable "noncurrent_version_expiration_days" {
  description = "Days after which noncurrent document object versions are expired from S3."
  type        = number
  default     = 365
}

variable "abort_incomplete_multipart_days" {
  description = "Days after which incomplete multipart uploads are aborted."
  type        = number
  default     = 7
}
