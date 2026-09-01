variable "aws_region" {
  description = "AWS region used for all resources (LocalStack compatible)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a default tag and used as a name prefix."
  type        = string
  default     = "bookmark-manager-api"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table storing bookmark records."
  type        = string
  default     = "bookmarks"
}

variable "dynamodb_tag_index_name" {
  description = "Name of the global secondary index used for tag filtering."
  type        = string
  default     = "bookmarks-tag-index"
}

variable "api_key_secret_name" {
  description = "Secrets Manager secret name holding the shared API key JSON document."
  type        = string
  default     = "bookmark-manager/api-key"
}

variable "api_key" {
  description = "Shared API key value stored in Secrets Manager and expected in the X-API-Key header."
  type        = string
  default     = "cloudforge-bookmark-manager-local-key"
  sensitive   = true
}

variable "log_group_name" {
  description = "CloudWatch Logs group for application request and error logs."
  type        = string
  default     = "/cloudforge/bookmark-manager-api"
}

variable "log_retention_in_days" {
  description = "Retention period, in days, for the application log group."
  type        = number
  default     = 365
}

variable "app_role_name" {
  description = "IAM role assumed by the running bookmark manager service."
  type        = string
  default     = "bookmark-manager-app-role"
}

variable "app_policy_name" {
  description = "IAM policy granting the service least-privilege access to its dependencies."
  type        = string
  default     = "bookmark-manager-app-policy"
}
