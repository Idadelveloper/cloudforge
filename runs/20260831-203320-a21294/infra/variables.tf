variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default region)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Logical project name, used for tagging and resource naming."
  type        = string
  default     = "contact_form_backend"
}

variable "managed_by" {
  description = "Value for the managed-by tag applied to every resource."
  type        = string
  default     = "cloudforge"
}

variable "messages_table_name" {
  description = "Name of the DynamoDB table that stores contact form messages."
  type        = string
  default     = "contact-messages"
}

variable "app_role_name" {
  description = "Name of the IAM role assumed by the contact form backend service."
  type        = string
  default     = "contact-form-app-role"
}

variable "app_policy_name" {
  description = "Name of the least-privilege IAM policy attached to the application role."
  type        = string
  default     = "contact-form-app-policy"
}

variable "log_group_name" {
  description = "CloudWatch Logs group the application writes structured logs to."
  type        = string
  default     = "/cloudforge/contact-form-backend"
}

variable "log_retention_days" {
  description = "Retention period in days for the application log group."
  type        = number
  default     = 365
}
