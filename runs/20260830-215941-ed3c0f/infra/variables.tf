variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default region)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier used for tagging and resource naming."
  type        = string
  default     = "personal-notes-api"
}

variable "managed_by" {
  description = "Value for the managed-by default tag."
  type        = string
  default     = "cloudforge"
}

variable "notes_table_name" {
  description = "Name of the DynamoDB table holding note items (matches the plan's DynamoDB resource 'notes')."
  type        = string
  default     = "notes"
}

variable "log_group_name" {
  description = "CloudWatch Logs group the FastAPI service writes application/request logs to."
  type        = string
  default     = "/cloudforge/personal-notes-api"
}

variable "log_retention_days" {
  description = "Retention period, in days, for the application log group."
  type        = number
  default     = 365
}

variable "app_policy_name" {
  description = "Name of the least-privilege IAM policy used by the notes API service."
  type        = string
  default     = "notes-api-app-policy"
}

variable "app_role_name" {
  description = "Name of the IAM role the notes API service assumes to obtain credentials."
  type        = string
  default     = "notes-api-app-role"
}

variable "kms_key_alias" {
  description = "Alias for the customer managed KMS key that encrypts the notes table and log group."
  type        = string
  default     = "alias/personal-notes-api"
}

variable "kms_deletion_window_in_days" {
  description = "Waiting period before the KMS key is deleted."
  type        = number
  default     = 7
}
