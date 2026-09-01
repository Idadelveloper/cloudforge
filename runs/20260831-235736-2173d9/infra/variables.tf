variable "aws_region" {
  description = "AWS region used for all resources of the event registration service."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a default tag and used as a name prefix."
  type        = string
  default     = "event-registration-service"
}

variable "environment" {
  description = "Deployment environment name (used only for tagging)."
  type        = string
  default     = "local"
}

variable "events_table_name" {
  description = "Name of the DynamoDB table storing event records."
  type        = string
  default     = "events"
}

variable "registrations_table_name" {
  description = "Name of the DynamoDB table storing attendee registrations."
  type        = string
  default     = "registrations"
}

variable "registrations_email_index_name" {
  description = "Name of the GSI used for duplicate attendee-email checks."
  type        = string
  default     = "attendee_email_index"
}

variable "registration_queue_name" {
  description = "Name of the SQS queue that receives a message for every successful registration."
  type        = string
  default     = "registration-events"
}

variable "registration_dlq_name" {
  description = "Name of the dead-letter queue backing the registration events queue."
  type        = string
  default     = "registration-events-dlq"
}

variable "service_role_name" {
  description = "Name of the IAM role assumed by the backend service."
  type        = string
  default     = "event-registration-service-role"
}

variable "log_group_name" {
  description = "CloudWatch Logs group used for application logs."
  type        = string
  default     = "/cloudforge/event-registration-service"
}

variable "log_retention_in_days" {
  description = "Retention period (days) for the application log group. Must be at least 365 to satisfy security baselines."
  type        = number
  default     = 365
}

variable "queue_visibility_timeout_seconds" {
  description = "Visibility timeout for the registration events queue."
  type        = number
  default     = 30
}

variable "queue_message_retention_seconds" {
  description = "Message retention period for the registration events queue (4 days)."
  type        = number
  default     = 345600
}

variable "dlq_message_retention_seconds" {
  description = "Message retention period for the dead-letter queue (14 days)."
  type        = number
  default     = 1209600
}

variable "queue_max_receive_count" {
  description = "Number of receives before a registration message is moved to the dead-letter queue."
  type        = number
  default     = 5
}

variable "queue_depth_alarm_threshold" {
  description = "Number of visible messages on the registration queue that triggers the backlog alarm."
  type        = number
  default     = 100
}

variable "rejected_registrations_alarm_threshold" {
  description = "Number of rejected registrations within the evaluation period that triggers the alarm."
  type        = number
  default     = 25
}

variable "metric_namespace" {
  description = "CloudWatch namespace for custom application metrics derived from logs."
  type        = string
  default     = "CloudForge/EventRegistration"
}
