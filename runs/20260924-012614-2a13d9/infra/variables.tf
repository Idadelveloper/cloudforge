variable "aws_region" {
  description = "AWS region used for all resources of the personal notes API."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as the 'project' default tag on every resource."
  type        = string
  default     = "personal_notes_api"
}

variable "managed_by" {
  description = "Value of the 'managed-by' default tag applied to every resource."
  type        = string
  default     = "cloudforge-terraform"
}

variable "notes_table_name" {
  description = "Name of the DynamoDB table that stores notes (partition key note_id). Supplied to the application via NOTES_TABLE_NAME."
  type        = string
  default     = "notes"

  validation {
    condition     = length(var.notes_table_name) >= 3 && length(var.notes_table_name) <= 255
    error_message = "The DynamoDB table name must be between 3 and 255 characters long."
  }
}

variable "notes_table_hash_key" {
  description = "Partition key attribute name of the notes table."
  type        = string
  default     = "note_id"
}

variable "dynamodb_billing_mode" {
  description = "Billing mode for the notes table. PAY_PER_REQUEST matches the personal-scale, unpredictable traffic of the API."
  type        = string
  default     = "PAY_PER_REQUEST"

  validation {
    condition     = contains(["PAY_PER_REQUEST", "PROVISIONED"], var.dynamodb_billing_mode)
    error_message = "dynamodb_billing_mode must be either PAY_PER_REQUEST or PROVISIONED."
  }
}

variable "enable_deletion_protection" {
  description = "Whether DynamoDB deletion protection is enabled on the notes table. Disabled by default so ephemeral test environments can be torn down."
  type        = bool
  default     = false
}

variable "app_role_name" {
  description = "Name of the IAM role assumed by the notes API application."
  type        = string
  default     = "notes-api-app-role"
}

variable "app_role_trusted_services" {
  description = "AWS service principals allowed to assume the application role that carries the least-privilege notes permissions."
  type        = list(string)
  default     = ["ec2.amazonaws.com"]
}

variable "log_group_name" {
  description = "CloudWatch Logs group the notes API writes request and error logs to."
  type        = string
  default     = "/cloudforge/personal-notes-api"

  validation {
    condition     = can(regex("^/", var.log_group_name))
    error_message = "log_group_name must start with a forward slash."
  }
}

variable "log_retention_days" {
  description = "Retention period, in days, for the notes API CloudWatch log group."
  type        = number
  default     = 365

  validation {
    condition     = var.log_retention_days >= 365
    error_message = "Logs must be retained for at least 365 days."
  }
}

variable "kms_key_deletion_window_days" {
  description = "Waiting period, in days, before the customer managed KMS key is deleted."
  type        = number
  default     = 30

  validation {
    condition     = var.kms_key_deletion_window_days >= 7 && var.kms_key_deletion_window_days <= 30
    error_message = "kms_key_deletion_window_days must be between 7 and 30."
  }
}
