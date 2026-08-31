variable "aws_region" {
  description = "AWS region used for all resources of the personal notes API."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied to resource names and default tags."
  type        = string
  default     = "personal-notes-api"
}

variable "managed_by" {
  description = "Value of the managed-by default tag."
  type        = string
  default     = "terraform"
}

variable "notes_table_name" {
  description = "Name of the DynamoDB table storing notes (plan resource: notes-table)."
  type        = string
  default     = "notes-table"
}

variable "lambda_function_name" {
  description = "Name of the Lambda function hosting the notes API (plan resource: notes-api-function)."
  type        = string
  default     = "notes-api-function"
}

variable "api_gateway_name" {
  description = "Name of the REST API fronting the notes Lambda (plan resource: notes-api-gateway)."
  type        = string
  default     = "notes-api-gateway"
}

variable "lambda_role_name" {
  description = "Name of the Lambda execution role (plan resource: notes-api-lambda-role)."
  type        = string
  default     = "notes-api-lambda-role"
}

variable "api_gateway_log_group_name" {
  description = "CloudWatch log group name for API Gateway access logs (plan resource: notes-api-log-group)."
  type        = string
  default     = "/aws/apigateway/notes-api-log-group"
}

variable "log_retention_in_days" {
  description = "Retention period, in days, for the CloudWatch log groups (>= 365 for compliance)."
  type        = number
  default     = 365
}

variable "api_stage_name" {
  description = "Deployment stage name for the REST API."
  type        = string
  default     = "prod"
}

variable "lambda_runtime" {
  description = "Python runtime used by the notes API Lambda function."
  type        = string
  default     = "python3.11"
}

variable "lambda_memory_size" {
  description = "Memory (MB) allocated to the notes API Lambda function."
  type        = number
  default     = 512
}

variable "lambda_timeout" {
  description = "Timeout (seconds) for the notes API Lambda function."
  type        = number
  default     = 30
}

variable "lambda_reserved_concurrency" {
  description = "Reserved concurrent executions for the notes API Lambda function."
  type        = number
  default     = 10
}

variable "default_user_id" {
  description = "Partition key value used when no X-User-Id header is supplied."
  type        = string
  default     = "default-user"
}
