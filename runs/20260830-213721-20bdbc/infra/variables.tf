variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Logical project name, used for tagging and resource naming."
  type        = string
  default     = "personal-notes-api"
}

variable "managed_by" {
  description = "Value for the managed-by tag applied to every resource."
  type        = string
  default     = "cloudforge-terraform"
}

variable "notes_table_name" {
  description = "Name of the DynamoDB table that stores notes (NOTES_TABLE_NAME for the app)."
  type        = string
  default     = "notes"
}

variable "notes_table_hash_key" {
  description = "Partition key attribute name for the notes table."
  type        = string
  default     = "note_id"
}

variable "service_role_name" {
  description = "Name of the IAM role assumed by the notes API service."
  type        = string
  default     = "notes_api_service_role"
}

variable "service_policy_name" {
  description = "Name of the least-privilege IAM policy attached to the service role."
  type        = string
  default     = "notes_api_service_policy"
}

variable "application_log_group_name" {
  description = "CloudWatch Logs group the FastAPI service writes structured logs to."
  type        = string
  default     = "/personal-notes-api/application"
}

variable "log_retention_in_days" {
  description = "Retention period for the application log group."
  type        = number
  default     = 365
}
