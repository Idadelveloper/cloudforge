variable "aws_region" {
  description = "AWS region used for every resource in the order processing service."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a default tag and used for resource naming context."
  type        = string
  default     = "order-processing-service"
}

variable "environment" {
  description = "Deployment environment name (used only for tagging)."
  type        = string
  default     = "dev"
}

variable "managed_by" {
  description = "Value of the managed-by default tag."
  type        = string
  default     = "terraform"
}

variable "orders_table_name" {
  description = "Name of the DynamoDB table that stores order records."
  type        = string
  default     = "orders"
}

variable "orders_customer_index_name" {
  description = "Name of the DynamoDB global secondary index used to list orders by customer."
  type        = string
  default     = "customer_id-created_at-index"
}

variable "fulfilment_queue_name" {
  description = "Name of the SQS queue carrying asynchronous fulfilment messages."
  type        = string
  default     = "order-fulfilment-queue"
}

variable "fulfilment_dlq_name" {
  description = "Name of the SQS dead letter queue for the fulfilment queue."
  type        = string
  default     = "order-fulfilment-dlq"
}

variable "order_status_topic_name" {
  description = "Name of the SNS topic that broadcasts order status change events."
  type        = string
  default     = "order-status-topic"
}

variable "fulfilment_worker_function_name" {
  description = "Name of the Lambda function that consumes the fulfilment queue."
  type        = string
  default     = "order-fulfilment-worker"
}

variable "fulfilment_worker_role_name" {
  description = "Name of the IAM execution role for the fulfilment Lambda worker."
  type        = string
  default     = "order-fulfilment-worker-role"
}

variable "app_policy_name" {
  description = "Name of the least-privilege IAM policy used by the standalone backend service."
  type        = string
  default     = "order-service-app-policy"
}

variable "fulfilment_worker_log_group_name" {
  description = "CloudWatch Logs group for the fulfilment Lambda worker."
  type        = string
  default     = "/aws/lambda/order-fulfilment-worker"
}

variable "log_retention_days" {
  description = "Retention period (days) for the fulfilment worker log group."
  type        = number
  default     = 365
}

variable "lambda_runtime" {
  description = "Python runtime used by the fulfilment worker Lambda."
  type        = string
  default     = "python3.11"
}

variable "lambda_timeout_seconds" {
  description = "Timeout in seconds for the fulfilment worker Lambda."
  type        = number
  default     = 30
}

variable "lambda_memory_size" {
  description = "Memory (MB) allocated to the fulfilment worker Lambda."
  type        = number
  default     = 256
}

variable "lambda_reserved_concurrency" {
  description = "Function level reserved concurrency for the fulfilment worker Lambda."
  type        = number
  default     = 5
}

variable "lambda_batch_size" {
  description = "Number of SQS messages delivered to the worker per invocation."
  type        = number
  default     = 10
}

variable "fulfilment_target_status" {
  description = "Order status the fulfilment worker sets once it picks up a message."
  type        = string
  default     = "PROCESSING"
}

variable "queue_visibility_timeout_seconds" {
  description = "Visibility timeout for the fulfilment queue (must exceed the Lambda timeout)."
  type        = number
  default     = 180
}

variable "queue_message_retention_seconds" {
  description = "Message retention period for the fulfilment queue."
  type        = number
  default     = 345600
}

variable "dlq_message_retention_seconds" {
  description = "Message retention period for the fulfilment dead letter queue."
  type        = number
  default     = 1209600
}

variable "queue_max_receive_count" {
  description = "Number of receives before a fulfilment message is redriven to the DLQ."
  type        = number
  default     = 5
}
