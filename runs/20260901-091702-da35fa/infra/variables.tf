variable "aws_region" {
  description = "AWS region used for every resource in the order processing service."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a default tag on every resource."
  type        = string
  default     = "order-processing-service"
}

variable "orders_table_name" {
  description = "Name of the DynamoDB table holding order records."
  type        = string
  default     = "orders"
}

variable "orders_customer_index_name" {
  description = "Name of the DynamoDB global secondary index used to list orders by customer."
  type        = string
  default     = "customer_id-created_at-index"
}

variable "fulfillment_queue_name" {
  description = "Name of the SQS queue carrying asynchronous order fulfilment messages."
  type        = string
  default     = "order-fulfillment-queue"
}

variable "fulfillment_dlq_name" {
  description = "Name of the SQS dead-letter queue for fulfilment messages that cannot be processed."
  type        = string
  default     = "order-fulfillment-dlq"
}

variable "status_topic_name" {
  description = "Name of the SNS topic that notifies subscribers of order status changes."
  type        = string
  default     = "order-status-changed-topic"
}

variable "worker_function_name" {
  description = "Name of the Lambda function that consumes fulfilment messages from SQS."
  type        = string
  default     = "order-fulfillment-worker"
}

variable "worker_role_name" {
  description = "Name of the IAM execution role for the fulfilment worker Lambda."
  type        = string
  default     = "order-fulfillment-worker-role"
}

variable "api_service_policy_name" {
  description = "Name of the least-privilege IAM policy used by the FastAPI order service."
  type        = string
  default     = "order-api-service-policy"
}

variable "queue_visibility_timeout_seconds" {
  description = "Visibility timeout of the fulfilment queue in seconds."
  type        = number
  default     = 30
}

variable "queue_message_retention_seconds" {
  description = "Retention period for messages on the fulfilment queue in seconds."
  type        = number
  default     = 345600
}

variable "dlq_message_retention_seconds" {
  description = "Retention period for messages on the dead-letter queue in seconds."
  type        = number
  default     = 1209600
}

variable "max_receive_count" {
  description = "Number of delivery attempts before a fulfilment message is moved to the dead-letter queue."
  type        = number
  default     = 3
}

variable "worker_runtime" {
  description = "Lambda runtime for the fulfilment worker."
  type        = string
  default     = "python3.11"
}

variable "worker_timeout_seconds" {
  description = "Timeout of the fulfilment worker Lambda (must not exceed the queue visibility timeout)."
  type        = number
  default     = 25
}

variable "worker_memory_size" {
  description = "Memory size in MB allocated to the fulfilment worker Lambda."
  type        = number
  default     = 256
}

variable "worker_reserved_concurrency" {
  description = "Reserved concurrent executions for the fulfilment worker Lambda."
  type        = number
  default     = 5
}

variable "worker_batch_size" {
  description = "Maximum number of SQS records delivered to the worker per invocation."
  type        = number
  default     = 5
}

variable "log_retention_days" {
  description = "Retention in days for the fulfilment worker CloudWatch log group (at least one year to satisfy log retention policy)."
  type        = number
  default     = 365
}
