variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default region)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a tag to every resource."
  type        = string
  default     = "shop-inventory-api"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "table_name" {
  description = "Name of the DynamoDB table that stores product records."
  type        = string
  default     = "products"
}

variable "sku_index_name" {
  description = "Name of the global secondary index used for SKU lookups and duplicate detection."
  type        = string
  default     = "sku-index"
}

variable "role_name" {
  description = "Name of the IAM role assumed by the inventory API service."
  type        = string
  default     = "shop-inventory-api-role"
}

variable "log_group_name" {
  description = "CloudWatch Logs group the application writes structured request and stock-adjustment logs to."
  type        = string
  default     = "/shop-inventory-api/application"
}

variable "log_retention_days" {
  description = "Retention period in days for the application log group (>= 365 for audit retention)."
  type        = number
  default     = 365
}

variable "kms_key_alias" {
  description = "Alias for the customer managed KMS key protecting the table and log data."
  type        = string
  default     = "alias/shop-inventory-api"
}
