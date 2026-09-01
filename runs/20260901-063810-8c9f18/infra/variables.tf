variable "aws_region" {
  description = "AWS region used for all resources."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Logical project name applied as a tag to every resource."
  type        = string
  default     = "product-feedback-service"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table storing feedback records."
  type        = string
  default     = "product-feedback"
}

variable "dynamodb_gsi_name" {
  description = "Name of the global secondary index used to list/aggregate feedback per product."
  type        = string
  default     = "product_id-created_at-index"
}

variable "sns_topic_name" {
  description = "Name of the SNS topic that receives low-rating (1-2 star) alerts."
  type        = string
  default     = "low-rating-alerts"
}

variable "service_role_name" {
  description = "Name of the IAM role assumed by the feedback service."
  type        = string
  default     = "product-feedback-service-role"
}

variable "log_group_name" {
  description = "CloudWatch Logs group for application logs."
  type        = string
  default     = "/product-feedback/service-logs"
}

variable "log_retention_in_days" {
  description = "Retention period, in days, for the application log group."
  type        = number
  default     = 365
}
