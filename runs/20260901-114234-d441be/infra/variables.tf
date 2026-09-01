variable "aws_region" {
  description = "AWS region used for every resource in this stack."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a default tag to every resource."
  type        = string
  default     = "loyalty-points-service"
}

variable "s3_use_path_style" {
  description = "Use path-style S3 addressing (required by LocalStack)."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------

variable "customers_table_name" {
  description = "DynamoDB table holding customer loyalty accounts and point balances."
  type        = string
  default     = "loyalty-customers"
}

variable "transactions_table_name" {
  description = "DynamoDB table holding the append-only point transaction ledger."
  type        = string
  default     = "loyalty-transactions"
}

variable "idempotency_table_name" {
  description = "DynamoDB table holding purchase idempotency keys (TTL protected)."
  type        = string
  default     = "loyalty-idempotency"
}

variable "idempotency_ttl_attribute" {
  description = "Attribute name used as the DynamoDB TTL field on the idempotency table."
  type        = string
  default     = "expires_at"
}

# ---------------------------------------------------------------------------
# SQS
# ---------------------------------------------------------------------------

variable "purchases_queue_name" {
  description = "SQS queue buffering accepted purchases for asynchronous point accrual."
  type        = string
  default     = "loyalty-purchases-queue"
}

variable "purchases_dlq_name" {
  description = "SQS dead-letter queue for purchase messages that repeatedly fail accrual."
  type        = string
  default     = "loyalty-purchases-dlq"
}

variable "purchases_queue_visibility_timeout" {
  description = "Visibility timeout (seconds) for the purchases queue."
  type        = number
  default     = 60
}

variable "purchases_queue_max_receive_count" {
  description = "Number of receives before a purchase message is moved to the DLQ."
  type        = number
  default     = 5
}

# ---------------------------------------------------------------------------
# SNS
# ---------------------------------------------------------------------------

variable "gold_upgrades_topic_name" {
  description = "SNS topic announcing gold-tier upgrades when a balance crosses 1000 points."
  type        = string
  default     = "loyalty-gold-tier-upgrades"
}

# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

variable "audit_bucket_name" {
  description = "S3 bucket where every balance change is appended as a JSON audit object."
  type        = string
  default     = "loyalty-audit-log"
}

variable "audit_log_prefix" {
  description = "Key prefix under which audit objects are written in the audit bucket."
  type        = string
  default     = "audit/"
}

variable "access_logs_bucket_name" {
  description = "S3 bucket receiving server access logs for the audit bucket."
  type        = string
  default     = "loyalty-audit-log-access-logs"
}

variable "audit_noncurrent_version_expiration_days" {
  description = "Days after which non-current audit object versions are expired."
  type        = number
  default     = 365
}

# ---------------------------------------------------------------------------
# CloudWatch
# ---------------------------------------------------------------------------

variable "log_group_name" {
  description = "CloudWatch log group the loyalty service writes structured logs to."
  type        = string
  default     = "/loyalty/points-service"
}

variable "log_retention_days" {
  description = "Retention (days) for the service log group."
  type        = number
  default     = 365
}

variable "dlq_alarm_threshold" {
  description = "Visible-message count in the DLQ that triggers the CloudWatch alarm."
  type        = number
  default     = 1
}

# ---------------------------------------------------------------------------
# Secrets Manager
# ---------------------------------------------------------------------------

variable "service_config_secret_name" {
  description = "Secrets Manager secret holding the service runtime config (shared API key)."
  type        = string
  default     = "loyalty-service-config"
}

variable "secret_recovery_window_days" {
  description = "Recovery window in days for the service config secret (0 = delete immediately)."
  type        = number
  default     = 0
}

# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------

variable "service_role_name" {
  description = "IAM role assumed by the loyalty points backend service."
  type        = string
  default     = "loyalty-service-role"
}

variable "gold_tier_threshold" {
  description = "Point balance at which a customer is upgraded to gold tier (exported for the app)."
  type        = number
  default     = 1000
}
