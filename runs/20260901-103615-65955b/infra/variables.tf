variable "aws_region" {
  description = "AWS region used for all notification hub resources."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a default tag on every resource."
  type        = string
  default     = "notification-hub"
}

variable "topic_name" {
  description = "Name of the central SNS topic that services publish events to."
  type        = string
  default     = "notification-hub-events"
}

variable "email_queue_name" {
  description = "Name of the SQS queue backing the email-like delivery channel."
  type        = string
  default     = "notification-hub-email-queue"
}

variable "webhook_queue_name" {
  description = "Name of the SQS queue backing the webhook-like delivery channel."
  type        = string
  default     = "notification-hub-webhook-queue"
}

variable "dlq_name" {
  description = "Name of the shared dead letter queue for undeliverable notifications."
  type        = string
  default     = "notification-hub-dlq"
}

variable "subscriptions_table_name" {
  description = "Name of the DynamoDB table storing subscription records."
  type        = string
  default     = "notification-hub-subscriptions"
}

variable "subscriptions_channel_index_name" {
  description = "Name of the global secondary index used to list subscriptions by channel."
  type        = string
  default     = "channel-index"
}

variable "log_group_name" {
  description = "CloudWatch Logs group used for application publish/subscription tracing."
  type        = string
  default     = "/aws/notification-hub/app"
}

variable "log_retention_in_days" {
  description = "Retention period for the application log group."
  type        = number
  default     = 365
}

variable "app_role_name" {
  description = "Name of the IAM role assumed by the notification hub backend."
  type        = string
  default     = "notification-hub-app-role"
}

variable "app_policy_name" {
  description = "Name of the least privilege IAM policy for the notification hub backend."
  type        = string
  default     = "notification-hub-app-policy"
}

variable "queue_visibility_timeout_seconds" {
  description = "Visibility timeout applied to the channel queues and the dead letter queue."
  type        = number
  default     = 30
}

variable "channel_queue_retention_seconds" {
  description = "Message retention period for the channel queues (seconds)."
  type        = number
  default     = 345600
}

variable "max_receive_count" {
  description = "Number of receives before a notification is moved to the dead letter queue."
  type        = number
  default     = 5
}
