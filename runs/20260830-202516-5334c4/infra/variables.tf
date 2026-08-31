variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default endpoint region)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a default tag on every resource."
  type        = string
  default     = "personal-notes-api"
}

variable "managed_by" {
  description = "Value for the managed-by default tag."
  type        = string
  default     = "cloudforge-terraform"
}

variable "table_name" {
  description = "Name of the DynamoDB table that stores notes (plan resource: notes-table)."
  type        = string
  default     = "notes-table"
}

variable "lambda_function_name" {
  description = "Name of the Lambda function hosting the notes API (plan resource: notes-api-fn)."
  type        = string
  default     = "notes-api-fn"
}

variable "lambda_role_name" {
  description = "Name of the Lambda execution role (plan resource: notes-api-lambda-role)."
  type        = string
  default     = "notes-api-lambda-role"
}

variable "api_name" {
  description = "Name of the REST API Gateway (plan resource: notes-api-gateway)."
  type        = string
  default     = "notes-api-gateway"
}

variable "api_gateway_cloudwatch_role_name" {
  description = "Name of the IAM role API Gateway uses to push access logs to CloudWatch Logs."
  type        = string
  default     = "notes-api-gateway-logs-role"
}

variable "lambda_log_group_name" {
  description = "CloudWatch log group for the Lambda function (plan resource: notes-api-log-group)."
  type        = string
  default     = "/aws/lambda/notes-api-fn"
}

variable "api_log_group_name" {
  description = "CloudWatch log group for API Gateway access logs (plan resource: notes-api-log-group)."
  type        = string
  default     = "/aws/apigateway/notes-api-gateway"
}

variable "log_retention_days" {
  description = "Retention in days for the CloudWatch log groups."
  type        = number
  default     = 30
}

variable "stage_name" {
  description = "API Gateway deployment stage name."
  type        = string
  default     = "dev"
}

variable "lambda_runtime" {
  description = "Python runtime for the notes API Lambda function."
  type        = string
  default     = "python3.11"
}

variable "lambda_handler" {
  description = "Handler entrypoint of the packaged Lambda source file."
  type        = string
  default     = "handler.lambda_handler"
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds."
  type        = number
  default     = 30
}

variable "lambda_memory_size" {
  description = "Lambda memory size in MB."
  type        = number
  default     = 512
}

variable "lambda_reserved_concurrency" {
  description = "Reserved concurrent executions for the Lambda function (limits blast radius)."
  type        = number
  default     = 10
}

variable "default_owner_id" {
  description = "Constant owner partition key used while the API is single tenant."
  type        = string
  default     = "default-user"
}

variable "default_page_size" {
  description = "Default page size for GET /notes."
  type        = number
  default     = 25
}

variable "max_page_size" {
  description = "Maximum page size for GET /notes."
  type        = number
  default     = 100
}

variable "log_level" {
  description = "Log level for the Lambda function."
  type        = string
  default     = "INFO"
}
