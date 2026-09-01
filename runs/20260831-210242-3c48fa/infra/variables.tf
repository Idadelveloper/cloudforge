variable "aws_region" {
  description = "AWS region used for all contact-form backend resources."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a default tag and used as a name prefix."
  type        = string
  default     = "contact-form-backend"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table that stores contact-form messages."
  type        = string
  default     = "contact-form-messages"
}

variable "dynamodb_hash_key" {
  description = "Partition key attribute name for the messages table."
  type        = string
  default     = "message_id"
}

variable "admin_api_key_secret_name" {
  description = "Secrets Manager secret name holding the shared administrator API key."
  type        = string
  default     = "contact-form/admin-api-key"
}

variable "log_group_name" {
  description = "CloudWatch Logs group the FastAPI service writes structured audit logs to."
  type        = string
  default     = "/contact-form/backend"
}

variable "log_retention_in_days" {
  description = "Retention period, in days, for the application log group (>= 365 for compliance)."
  type        = number
  default     = 365
}

variable "app_role_name" {
  description = "Name of the IAM role assumed by the standalone FastAPI service."
  type        = string
  default     = "contact-form-backend-app-role"
}

variable "secret_recovery_window_in_days" {
  description = "Recovery window for the admin API key secret when deleted. 0 forces immediate deletion (useful for local stacks)."
  type        = number
  default     = 0
}

variable "admin_api_key_length" {
  description = "Length of the generated administrator API key."
  type        = number
  default     = 48
}
