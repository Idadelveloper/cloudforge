variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default: us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Logical project name, applied as a default tag and used for resource naming."
  type        = string
  default     = "expense-tracker-api"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "expenses_table_name" {
  description = "Name of the DynamoDB table that stores expense records (EXPENSES_TABLE env var for the app)."
  type        = string
  default     = "expenses"
}

variable "category_index_name" {
  description = "Name of the global secondary index used for category-scoped queries."
  type        = string
  default     = "expenses-gsi-category"
}

variable "app_role_name" {
  description = "Name of the IAM role assumed by the expense tracker backend service."
  type        = string
  default     = "expense-tracker-app-role"
}

variable "dynamodb_policy_name" {
  description = "Name of the least-privilege IAM policy granting DynamoDB access to the service."
  type        = string
  default     = "expense-tracker-dynamodb-policy"
}

variable "log_group_name" {
  description = "CloudWatch Logs group receiving the application's structured request/error logs."
  type        = string
  default     = "/cloudforge/expense-tracker-api"
}

variable "log_retention_days" {
  description = "Retention period (days) for the application log group."
  type        = number
  default     = 365
}
