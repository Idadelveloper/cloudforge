variable "aws_region" {
  description = "AWS region to deploy resources into."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used for tagging."
  type        = string
  default     = "personal_notes_api"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table storing notes."
  type        = string
  default     = "notes"
}

variable "api_gateway_name" {
  description = "Name of the API Gateway REST API."
  type        = string
  default     = "personal-notes-api-gateway"
}

variable "lambda_function_name" {
  description = "Name of the Lambda function running the FastAPI app."
  type        = string
  default     = "personal-notes-handler"
}

variable "lambda_role_name" {
  description = "Name of the IAM role assumed by the Lambda function."
  type        = string
  default     = "personal-notes-lambda-role"
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days."
  type        = number
  default     = 365
}

variable "api_stage_name" {
  description = "Deployment stage name for the API Gateway."
  type        = string
  default     = "prod"
}
