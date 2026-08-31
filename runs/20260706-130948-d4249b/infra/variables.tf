variable "aws_region" {
  description = "AWS region to deploy resources into."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name used for tagging."
  type        = string
  default     = "personal_notes_api"
}

variable "table_name" {
  description = "Name of the DynamoDB table storing notes."
  type        = string
  default     = "notes"
}

variable "lambda_function_name" {
  description = "Name of the Lambda function hosting the FastAPI application."
  type        = string
  default     = "personal_notes_api_fn"
}

variable "api_name" {
  description = "Name of the API Gateway REST API."
  type        = string
  default     = "personal_notes_api_gw"
}

variable "iam_role_name" {
  description = "Name of the IAM role assumed by the Lambda function."
  type        = string
  default     = "personal_notes_api_role"
}

variable "log_group_name" {
  description = "CloudWatch log group name for the Lambda function."
  type        = string
  default     = "personal_notes_api_logs"
}

variable "stage_name" {
  description = "API Gateway deployment stage name."
  type        = string
  default     = "prod"
}

variable "log_retention_days" {
  description = "Retention period in days for CloudWatch log groups."
  type        = number
  default     = 365
}

variable "lambda_reserved_concurrency" {
  description = "Reserved concurrent executions for the Lambda function."
  type        = number
  default     = 5
}
