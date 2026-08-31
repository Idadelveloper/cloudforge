variable "aws_region" {
  description = "AWS region used for all resources."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name applied as a default tag to every resource."
  type        = string
  default     = "personal_notes_api"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table that stores notes (plan: notes-table)."
  type        = string
  default     = "notes-table"
}

variable "lambda_function_name" {
  description = "Name of the Lambda function hosting the notes API (plan: notes-api-function)."
  type        = string
  default     = "notes-api-function"
}

variable "lambda_log_group_name" {
  description = "CloudWatch log group for the notes API Lambda (plan: notes-api-log-group)."
  type        = string
  default     = "/aws/lambda/notes-api-function"
}

variable "api_gateway_name" {
  description = "Name of the REST API Gateway fronting the Lambda (plan: notes-api-gateway)."
  type        = string
  default     = "notes-api-gateway"
}

variable "api_gateway_log_group_name" {
  description = "CloudWatch log group for API Gateway access logs."
  type        = string
  default     = "/aws/apigateway/notes-api-gateway"
}

variable "lambda_role_name" {
  description = "Name of the Lambda execution role (plan: notes-api-lambda-role)."
  type        = string
  default     = "notes-api-lambda-role"
}

variable "api_stage_name" {
  description = "API Gateway stage name."
  type        = string
  default     = "prod"
}

variable "lambda_runtime" {
  description = "Python runtime for the notes API Lambda."
  type        = string
  default     = "python3.11"
}

variable "lambda_memory_size" {
  description = "Memory (MB) allocated to the notes API Lambda."
  type        = number
  default     = 512
}

variable "lambda_timeout" {
  description = "Timeout (seconds) for the notes API Lambda."
  type        = number
  default     = 30
}

variable "lambda_reserved_concurrency" {
  description = "Reserved concurrent executions for the notes API Lambda."
  type        = number
  default     = 10
}

variable "log_retention_days" {
  description = "Retention period, in days, for CloudWatch log groups."
  type        = number
  default     = 365
}

variable "default_user_id" {
  description = "Fallback user id used when no X-User-Id header is supplied."
  type        = string
  default     = "default-user"
}
