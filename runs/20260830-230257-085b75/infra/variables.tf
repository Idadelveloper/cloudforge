variable "aws_region" {
  description = "AWS region used for all resources of the URL shortener service."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Logical project name, applied as a tag and used to build resource names."
  type        = string
  default     = "url_shortener_api"
}

variable "environment" {
  description = "Deployment environment identifier."
  type        = string
  default     = "dev"
}

variable "managed_by" {
  description = "Owner/automation identifier applied as the managed-by tag."
  type        = string
  default     = "cloudforge"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table storing short code to long URL mappings."
  type        = string
  default     = "url_shortener_mappings"
}

variable "dynamodb_hash_key" {
  description = "Partition key attribute name of the mappings table (the base62 short code)."
  type        = string
  default     = "code"
}

variable "service_role_name" {
  description = "Name of the IAM role assumed by the URL shortener backend service."
  type        = string
  default     = "url_shortener_service_role"
}

variable "dynamodb_policy_name" {
  description = "Name of the least-privilege IAM policy granting DynamoDB access to the mappings table."
  type        = string
  default     = "url_shortener_dynamodb_policy"
}

variable "log_group_name" {
  description = "CloudWatch Logs log group the application writes structured request logs to."
  type        = string
  default     = "/cloudforge/url-shortener"
}

variable "log_retention_in_days" {
  description = "Retention period in days for the application log group."
  type        = number
  default     = 90
}

variable "kms_key_alias" {
  description = "Alias for the customer managed KMS key encrypting DynamoDB items and log data."
  type        = string
  default     = "alias/url-shortener"
}

variable "kms_key_deletion_window_in_days" {
  description = "Waiting period, in days, before the KMS key is deleted after destruction."
  type        = number
  default     = 30
}
