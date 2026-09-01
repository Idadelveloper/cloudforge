variable "aws_region" {
  description = "AWS region used for all shop inventory API resources."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied to resource names and default tags."
  type        = string
  default     = "shop-inventory-api"
}

variable "environment" {
  description = "Deployment environment name (used for tagging only)."
  type        = string
  default     = "dev"
}

variable "products_table_name" {
  description = "Name of the DynamoDB table that stores product records, keyed by SKU."
  type        = string
  default     = "shop-inventory-products"
}

variable "api_role_name" {
  description = "Name of the IAM role assumed by the FastAPI inventory service."
  type        = string
  default     = "shop-inventory-api-role"
}

variable "dynamodb_access_policy_name" {
  description = "Name of the least-privilege IAM policy granting the API access to the products table and its log group."
  type        = string
  default     = "shop-inventory-dynamodb-access-policy"
}

variable "log_group_name" {
  description = "CloudWatch Logs group the FastAPI service writes structured request and error logs to."
  type        = string
  default     = "/shop-inventory/api"
}

variable "log_retention_days" {
  description = "Retention period, in days, for the API CloudWatch log group."
  type        = number
  default     = 365
}

variable "kms_key_alias" {
  description = "Alias for the customer managed KMS key protecting the products table and API logs."
  type        = string
  default     = "alias/shop-inventory-api"
}

variable "kms_key_deletion_window_days" {
  description = "Waiting period, in days, before the customer managed KMS key is deleted."
  type        = number
  default     = 30
}
