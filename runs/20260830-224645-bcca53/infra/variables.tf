variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default is us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Logical application name, used for tagging and resource naming."
  type        = string
  default     = "todo_task_api"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table that stores to-do tasks."
  type        = string
  default     = "todo_tasks"
}

variable "dynamodb_hash_key" {
  description = "Partition key attribute name for the tasks table."
  type        = string
  default     = "task_id"
}

variable "app_policy_name" {
  description = "Name of the least-privilege IAM policy used by the FastAPI service."
  type        = string
  default     = "todo_task_api_app_policy"
}

variable "app_role_name" {
  description = "Name of the IAM role the FastAPI service assumes for its boto3 calls."
  type        = string
  default     = "todo_task_api_app_role"
}

variable "log_group_name" {
  description = "CloudWatch Logs group the application writes request and DynamoDB error events to."
  type        = string
  default     = "/todo-task-api/application"
}

variable "log_retention_in_days" {
  description = "Retention period, in days, for the application log group."
  type        = number
  default     = 365
}
