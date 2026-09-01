variable "aws_region" {
  description = "AWS region the notification hub infrastructure is deployed into."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a tag to every resource."
  type        = string
  default     = "notification-hub"
}

variable "environment" {
  description = "Deployment environment name used for tagging."
  type        = string
  default     = "local"
}

variable "sns_topic_name" {
  description = "Name of the central SNS topic that POST /events publishes to."
  type        = string
  default     = "notification-hub-events-topic"
}

variable "email_queue_name" {
  description = "Name of the SQS queue backing the 'email' channel."
  type        = string
  default     = "notification-hub-email-queue"
}

variable "webhook_queue_name" {
  description = "Name of the SQS queue backing the 'webhook' channel."
  type        = string
  default     = "notification-hub-webhook-queue"
}

variable "dlq_name" {
  description = "Name of the shared dead-letter queue used as redrive target for both channel queues."
  type        = string
  default     = "notification-hub-dlq"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table storing subscription records."
  type        = string
  default     = "notification-hub-subscriptions"
}

variable "dynamodb_channel_index_name" {
  description = "Name of the DynamoDB global secondary index used to list subscriptions by channel."
  type        = string
  default     = "channel-created_at-index"
}

variable "service_role_name" {
  description = "Name of the IAM role assumed by the notification hub backend process."
  type        = string
  default     = "notification-hub-service-role"
}

variable "service_policy_name" {
  description = "Name of the least-privilege IAM policy attached to the service role."
  type        = string
  default     = "notification-hub-service-policy"
}

variable "log_group_name" {
  description = "CloudWatch Logs group the application writes structured logs to."
  type        = string
  default     = "/notification-hub/application"
}

variable "log_retention_days" {
  description = "Retention period in days for the application log group."
  type        = number
  default     = 365
}

variable "queue_visibility_timeout_seconds" {
  description = "Visibility timeout applied to the channel queues."
  type        = number
  default     = 30
}

variable "queue_message_retention_seconds" {
  description = "How long messages are retained in the channel queues (seconds)."
  type        = number
  default     = 345600
}

variable "dlq_message_retention_seconds" {
  description = "How long messages are retained in the dead-letter queue (seconds)."
  type        = number
  default     = 1209600
}

variable "max_receive_count" {
  description = "Number of receives before a message is moved to the dead-letter queue."
  type        = number
  default     = 5
}

variable "kms_deletion_window_in_days" {
  description = "Waiting period before the customer managed KMS key is deleted."
  type        = number
  default     = 7
}
