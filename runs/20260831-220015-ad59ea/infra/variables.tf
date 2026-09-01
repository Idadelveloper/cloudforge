variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default: us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as the 'project' default tag on every resource."
  type        = string
  default     = "shop-inventory-api"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table that stores product records (partition key: sku)."
  type        = string
  default     = "shop-inventory-products"
}

variable "dynamodb_hash_key" {
  description = "Partition key attribute name for the products table."
  type        = string
  default     = "sku"
}

variable "app_role_name" {
  description = "Name of the IAM role the FastAPI service assumes to reach DynamoDB and CloudWatch Logs."
  type        = string
  default     = "shop-inventory-api-app-role"
}

variable "app_policy_name" {
  description = "Name of the least-privilege IAM policy attached to the application role."
  type        = string
  default     = "shop-inventory-api-dynamodb-policy"
}

variable "application_log_group_name" {
  description = "CloudWatch Logs group the application writes request and stock-adjustment audit logs to."
  type        = string
  default     = "/shop-inventory-api/application"
}

variable "log_retention_in_days" {
  description = "Retention period, in days, for the application CloudWatch log group."
  type        = number
  default     = 365
}

variable "kms_key_alias" {
  description = "Alias for the customer managed KMS key protecting the products table and application logs."
  type        = string
  default     = "alias/shop-inventory-api"
}

variable "kms_key_deletion_window_in_days" {
  description = "Waiting period before the customer managed KMS key is deleted."
  type        = number
  default     = 7
}
