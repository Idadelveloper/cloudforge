variable "aws_region" {
  description = "AWS region used for all url_shortener resources."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Logical project name, used for tagging and resource naming."
  type        = string
  default     = "url_shortener"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table storing short-code -> long-URL mappings."
  type        = string
  default     = "url_shortener_urls"
}

variable "dynamodb_hash_key" {
  description = "Partition key attribute name for the short URL table."
  type        = string
  default     = "code"
}

variable "app_role_name" {
  description = "Name of the IAM role assumed by the url_shortener backend process."
  type        = string
  default     = "url_shortener_app_role"
}

variable "app_policy_name" {
  description = "Name of the least-privilege IAM policy attached to the application role."
  type        = string
  default     = "url_shortener_app_policy"
}

variable "log_group_name" {
  description = "CloudWatch Logs group the application writes operational logs to."
  type        = string
  default     = "/cloudforge/url_shortener"
}

variable "log_retention_days" {
  description = "Retention period in days for the application log group."
  type        = number
  default     = 365
}

variable "managed_by" {
  description = "Value for the managed-by tag applied to every resource."
  type        = string
  default     = "cloudforge-terraform"
}
