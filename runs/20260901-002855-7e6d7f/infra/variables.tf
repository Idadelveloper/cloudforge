variable "aws_region" {
  description = "AWS region used for all expense-tracker resources."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied to resource names and default tags."
  type        = string
  default     = "expense-tracker-api"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table that stores expense records (plan: 'expenses')."
  type        = string
  default     = "expenses"
}

variable "month_index_name" {
  description = "Global secondary index used for month based queries (plan: 'month-date-index')."
  type        = string
  default     = "month-date-index"
}

variable "category_index_name" {
  description = "Global secondary index used for category based queries (plan: 'category-date-index')."
  type        = string
  default     = "category-date-index"
}

variable "log_group_name" {
  description = "CloudWatch Logs group receiving structured application logs."
  type        = string
  default     = "/cloudforge/expense-tracker-api"
}

variable "log_retention_days" {
  description = "Retention period (days) for the application log group."
  type        = number
  default     = 365
}

variable "iam_role_name" {
  description = "Name of the IAM role assumed by the expense tracker service."
  type        = string
  default     = "expense-tracker-app-role"
}

variable "iam_policy_name" {
  description = "Name of the least-privilege IAM policy attached to the service role."
  type        = string
  default     = "expense-tracker-app-policy"
}

variable "kms_key_alias" {
  description = "Alias for the customer managed KMS key encrypting the table and logs."
  type        = string
  default     = "alias/expense-tracker-api"
}

variable "kms_key_deletion_window_days" {
  description = "Waiting period (days) before the KMS key is deleted after destroy."
  type        = number
  default     = 7
}
