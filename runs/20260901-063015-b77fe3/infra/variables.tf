variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name applied as a default tag on every resource."
  type        = string
  default     = "product_feedback_service"
}

variable "managed_by" {
  description = "Value for the managed-by default tag."
  type        = string
  default     = "cloudforge-terraform"
}

variable "feedback_table_name" {
  description = "DynamoDB table that stores product feedback submissions."
  type        = string
  default     = "product-feedback"
}

variable "feedback_table_hash_key" {
  description = "Partition key attribute for the feedback table."
  type        = string
  default     = "feedback_id"
}

variable "feedback_product_index_name" {
  description = "Global secondary index used to list/aggregate feedback per product."
  type        = string
  default     = "product_id-created_at-index"
}

variable "low_rating_topic_name" {
  description = "SNS topic that receives low-rating (1-2) feedback alerts."
  type        = string
  default     = "product-feedback-low-rating-alerts"
}

variable "sns_kms_master_key_id" {
  description = "KMS key alias used for SNS server-side encryption."
  type        = string
  default     = "alias/aws/sns"
}

variable "service_role_name" {
  description = "IAM role assumed by the feedback service runtime."
  type        = string
  default     = "product-feedback-service-role"
}

variable "log_group_name" {
  description = "CloudWatch Logs group for application logs."
  type        = string
  default     = "/cloudforge/product-feedback-service"
}

variable "log_retention_in_days" {
  description = "Retention period for the application log group."
  type        = number
  default     = 90
}
