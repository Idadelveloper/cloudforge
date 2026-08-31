variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default is us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name applied as a default tag on every resource."
  type        = string
  default     = "todo_api"
}

variable "managed_by" {
  description = "Value for the managed-by default tag."
  type        = string
  default     = "cloudforge-terraform"
}

variable "tasks_table_name" {
  description = "Name of the DynamoDB table holding to-do task records (matches TASKS_TABLE in the application)."
  type        = string
  default     = "tasks"
}

variable "tasks_table_hash_key" {
  description = "Partition key attribute name for the tasks table."
  type        = string
  default     = "task_id"
}

variable "app_role_name" {
  description = "Name of the IAM role assumed by the FastAPI backend service."
  type        = string
  default     = "todo_api_app_role"
}

variable "app_policy_name" {
  description = "Name of the least-privilege IAM policy granting access to the tasks table."
  type        = string
  default     = "todo_api_tasks_table_access"
}
