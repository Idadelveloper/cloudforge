###############################################################################
# Input variables - defaults match the resource names in the shared plan
###############################################################################

variable "aws_region" {
  description = "AWS region used for every resource in this stack."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier used for tagging and the KMS key alias."
  type        = string
  default     = "loyalty-points-service"
}

variable "customers_table_name" {
  description = "DynamoDB table holding customer loyalty accounts (balance and tier)."
  type        = string
  default     = "loyalty-customers"
}

variable "transactions_table_name" {
  description = "DynamoDB table holding per-customer purchase/accrual transactions."
  type        = string
  default     = "loyalty-transactions"
}

variable "idempotency_table_name" {
  description = "DynamoDB table holding idempotency keys for purchase submissions."
  type        = string
  default     = "loyalty-idempotency"
}

variable "purchases_queue_name" {
  description = "SQS queue that carries accepted purchases for asynchronous accrual."
  type        = string
  default     = "loyalty-purchases-queue"
}

variable "purchases_dlq_name" {
  description = "SQS dead-letter queue for purchase messages that repeatedly fail."
  type        = string
  default     = "loyalty-purchases-dlq"
}

variable "tier_upgrade_topic_name" {
  description = "SNS topic announcing gold-tier upgrades."
  type        = string
  default     = "loyalty-tier-upgrades"
}

variable "audit_bucket_name" {
  description = "S3 bucket holding the append-only balance-change audit log."
  type        = string
  default     = "loyalty-audit-log"
}

variable "audit_log_access_bucket_name" {
  description = "S3 bucket receiving server access logs for the audit-log bucket."
  type        = string
  default     = "loyalty-audit-log-access-logs"
}

variable "accrual_worker_function_name" {
  description = "Lambda function that consumes the purchase queue and applies points."
  type        = string
  default     = "loyalty-accrual-worker"
}

variable "accrual_worker_role_name" {
  description = "IAM execution role for the accrual worker Lambda."
  type        = string
  default     = "loyalty-accrual-worker-role"
}

variable "api_service_role_name" {
  description = "IAM identity used by the externally hosted FastAPI service."
  type        = string
  default     = "loyalty-api-service-user"
}

variable "api_config_secret_name" {
  description = "Secrets Manager secret holding the API shared secret and tuning values."
  type        = string
  default     = "loyalty-api-config"
}

variable "log_retention_days" {
  description = "Retention (days) for the accrual worker CloudWatch log group."
  type        = number
  default     = 365
}

variable "gold_tier_threshold" {
  description = "Point balance at which a customer is upgraded to the gold tier."
  type        = number
  default     = 1000
}

variable "points_per_dollar" {
  description = "Points awarded per whole USD of purchase amount."
  type        = number
  default     = 1
}

variable "queue_visibility_timeout_seconds" {
  description = "Visibility timeout for the purchases queue (>= Lambda timeout)."
  type        = number
  default     = 180
}

variable "queue_max_receive_count" {
  description = "Receive attempts before a purchase message is moved to the DLQ."
  type        = number
  default     = 5
}

variable "lambda_timeout_seconds" {
  description = "Timeout for the accrual worker Lambda."
  type        = number
  default     = 30
}

variable "lambda_memory_size" {
  description = "Memory (MB) for the accrual worker Lambda."
  type        = number
  default     = 256
}

variable "lambda_reserved_concurrency" {
  description = "Reserved concurrent executions for the accrual worker Lambda."
  type        = number
  default     = 5
}
