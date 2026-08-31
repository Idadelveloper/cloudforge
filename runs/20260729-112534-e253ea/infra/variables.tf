variable "aws_region" {
  description = "AWS region to deploy resources into."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name used for tagging and naming."
  type        = string
  default     = "url_shortener"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table storing URL mappings."
  type        = string
  default     = "url_mappings"
}

variable "lambda_function_name" {
  description = "Name of the Lambda function running the URL shortener app."
  type        = string
  default     = "url_shortener_fn"
}

variable "api_name" {
  description = "Name of the API Gateway HTTP API."
  type        = string
  default     = "url_shortener_api"
}

variable "lambda_role_name" {
  description = "Name of the IAM role assumed by the Lambda function."
  type        = string
  default     = "url_shortener_lambda_role"
}

variable "log_retention_days" {
  description = "Retention in days for CloudWatch log groups."
  type        = number
  default     = 365
}
