variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default region)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used for tagging and KMS alias naming."
  type        = string
  default     = "bookmark-manager-api"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "local"
}

variable "bookmarks_table_name" {
  description = "DynamoDB table holding bookmark items (partition key bookmark_id)."
  type        = string
  default     = "bookmarks"
}

variable "bookmark_tags_table_name" {
  description = "DynamoDB table holding one row per bookmark/tag pair (partition key tag, sort key bookmark_id)."
  type        = string
  default     = "bookmark_tags"
}

variable "api_key_secret_name" {
  description = "Secrets Manager secret name that stores the shared API key."
  type        = string
  default     = "bookmark-manager/api-key"
}

variable "api_key_value" {
  description = "Optional explicit API key value. When empty a random key is generated."
  type        = string
  default     = ""
  sensitive   = true
}

variable "app_role_name" {
  description = "IAM role assumed by the bookmark manager service."
  type        = string
  default     = "bookmark-manager-app-role"
}

variable "log_group_name" {
  description = "CloudWatch Logs group the application ships request and error logs to."
  type        = string
  default     = "/cloudforge/bookmark-manager"
}

variable "log_retention_days" {
  description = "Retention period in days for the application log group."
  type        = number
  default     = 365
}

variable "tag_created_at_index_name" {
  description = "Global secondary index on the bookmark_tags table used for newest-first tag listings."
  type        = string
  default     = "tag_created_at_index"
}
