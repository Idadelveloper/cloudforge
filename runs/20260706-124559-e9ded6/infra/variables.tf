variable "aws_region" {
  description = "AWS region to deploy resources into."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used for tagging and resource naming."
  type        = string
  default     = "personal_notes_api"
}

variable "notes_table_name" {
  description = "Name of the DynamoDB table that stores notes."
  type        = string
  default     = "notes_table"
}

variable "notes_api_role_name" {
  description = "Name of the IAM execution role for the notes API."
  type        = string
  default     = "notes_api_role"
}

variable "notes_api_log_group_name" {
  description = "Name of the CloudWatch log group for the notes API."
  type        = string
  default     = "/aws/personal_notes_api/notes_api_logs"
}

variable "log_retention_days" {
  description = "Retention period in days for the CloudWatch log group."
  type        = number
  default     = 365
}
