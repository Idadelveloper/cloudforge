variable "aws_region" {
  description = "AWS region used for all resources of the shop inventory API."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Logical project name, applied as a provider default tag."
  type        = string
  default     = "shop_inventory_api"
}

variable "managed_by" {
  description = "Value for the managed-by default tag."
  type        = string
  default     = "cloudforge-terraform"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "products_table_name" {
  description = "Name of the DynamoDB table that stores product records (partition key: sku)."
  type        = string
  default     = "products"
}

variable "app_role_name" {
  description = "Name of the IAM role assumed by the standalone backend service."
  type        = string
  default     = "shop_inventory_api_app_role"
}

variable "app_policy_name" {
  description = "Name of the least-privilege IAM policy attached to the application role."
  type        = string
  default     = "shop_inventory_api_app_policy"
}

variable "log_group_name" {
  description = "CloudWatch Logs group the application writes structured request and stock-adjustment logs to."
  type        = string
  default     = "/shop-inventory-api/application"
}

variable "log_retention_in_days" {
  description = "Retention period, in days, for the application log group."
  type        = number
  default     = 365
}

variable "kms_key_alias" {
  description = "Alias for the customer managed KMS key encrypting the DynamoDB table and log group."
  type        = string
  default     = "alias/shop-inventory-api"
}

variable "kms_key_deletion_window_in_days" {
  description = "Waiting period, in days, before the customer managed KMS key is deleted."
  type        = number
  default     = 30
}
