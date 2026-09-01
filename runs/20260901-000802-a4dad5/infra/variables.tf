variable "aws_region" {
  description = "AWS region used for all resources (LocalStack default is us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a default tag on every resource."
  type        = string
  default     = "event_registration_service"
}

variable "managed_by" {
  description = "Value for the managed-by default tag."
  type        = string
  default     = "cloudforge"
}

variable "events_table_name" {
  description = "Name of the DynamoDB table storing organiser-created events."
  type        = string
  default     = "events"
}

variable "registrations_table_name" {
  description = "Name of the DynamoDB table storing attendee registrations."
  type        = string
  default     = "registrations"
}

variable "registrations_email_index_name" {
  description = "Global secondary index used to detect duplicate registrations by attendee email."
  type        = string
  default     = "attendee_email-index"
}

variable "registration_queue_name" {
  description = "Name of the SQS queue that receives a message for every successful registration."
  type        = string
  default     = "registration-events"
}

variable "registration_dlq_name" {
  description = "Name of the dead-letter queue used as the redrive target of the registration queue."
  type        = string
  default     = "registration-events-dlq"
}

variable "service_role_name" {
  description = "Name of the IAM role assumed by the backend service."
  type        = string
  default     = "event-registration-service-role"
}

variable "log_group_name" {
  description = "CloudWatch Logs group for the application logs."
  type        = string
  default     = "/cloudforge/event-registration-service"
}

variable "log_retention_in_days" {
  description = "Retention period for the application log group (at least one year)."
  type        = number
  default     = 365
}

variable "queue_message_retention_seconds" {
  description = "How long the registration queue keeps unconsumed messages."
  type        = number
  default     = 345600
}

variable "dlq_message_retention_seconds" {
  description = "How long the dead-letter queue keeps messages."
  type        = number
  default     = 1209600
}

variable "queue_visibility_timeout_seconds" {
  description = "Visibility timeout for the registration queue."
  type        = number
  default     = 30
}

variable "queue_max_receive_count" {
  description = "Number of failed receives before a message is moved to the dead-letter queue."
  type        = number
  default     = 5
}
