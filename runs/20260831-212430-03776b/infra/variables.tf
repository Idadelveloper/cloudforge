variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default: us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier used for tagging and naming."
  type        = string
  default     = "contact-form-backend"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "managed_by" {
  description = "Value for the managed-by tag applied to every resource."
  type        = string
  default     = "cloudforge-terraform"
}

variable "messages_table_name" {
  description = "Name of the DynamoDB table storing contact-form messages."
  type        = string
  default     = "contact-form-messages"
}

variable "messages_table_hash_key" {
  description = "Partition key attribute name of the messages table."
  type        = string
  default     = "message_id"
}

variable "admin_api_key_secret_name" {
  description = "Secrets Manager secret name holding the admin API key."
  type        = string
  default     = "contact-form/admin-api-key"
}

variable "app_role_name" {
  description = "IAM role name assumed by the contact-form backend process."
  type        = string
  default     = "contact-form-backend-app-role"
}

variable "log_group_name" {
  description = "CloudWatch Logs group the application writes to."
  type        = string
  default     = "/contact-form/backend"
}

variable "log_retention_in_days" {
  description = "Retention period (days) for the application log group."
  type        = number
  default     = 365
}

variable "secret_recovery_window_in_days" {
  description = "Recovery window for the admin API key secret. 0 forces immediate deletion (useful for ephemeral environments)."
  type        = number
  default     = 0
}
