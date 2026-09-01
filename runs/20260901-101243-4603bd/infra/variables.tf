variable "aws_region" {
  description = "AWS region used for every resource in the async job processing stack."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier applied as a default tag and used for resource descriptions."
  type        = string
  default     = "async-job-processor"
}

variable "environment" {
  description = "Deployment environment name applied as a default tag."
  type        = string
  default     = "dev"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table holding job records (plan resource: jobs)."
  type        = string
  default     = "jobs"
}

variable "dynamodb_status_index_name" {
  description = "Global secondary index used by GET /jobs to filter by status ordered by creation time."
  type        = string
  default     = "status-created_at-index"
}

variable "job_queue_name" {
  description = "Name of the SQS queue the API writes to on job submission (plan resource: job-queue)."
  type        = string
  default     = "job-queue"
}

variable "job_dlq_name" {
  description = "Name of the dead-letter queue for jobs that fail after their single retry (plan resource: job-dlq)."
  type        = string
  default     = "job-dlq"
}

variable "max_receive_count" {
  description = "Redrive maxReceiveCount: 2 gives exactly one retry before a message is moved to the DLQ."
  type        = number
  default     = 2
}

variable "queue_visibility_timeout_seconds" {
  description = "Visibility timeout of the job queue; must be at least the worker Lambda timeout."
  type        = number
  default     = 180
}

variable "queue_message_retention_seconds" {
  description = "How long an unprocessed message stays on the job queue."
  type        = number
  default     = 345600
}

variable "dlq_message_retention_seconds" {
  description = "How long a message stays on the dead-letter queue for triage."
  type        = number
  default     = 1209600
}

variable "results_bucket_name" {
  description = "S3 bucket storing large job result payloads (plan resource: job-results)."
  type        = string
  default     = "job-results"
}

variable "log_bucket_name" {
  description = "S3 bucket receiving server access logs for the results bucket."
  type        = string
  default     = "job-results-access-logs"
}

variable "results_expiration_days" {
  description = "Number of days a job result object is retained before expiring."
  type        = number
  default     = 90
}

variable "lambda_function_name" {
  description = "Name of the SQS-triggered worker Lambda (plan resource: job-worker)."
  type        = string
  default     = "job-worker"
}

variable "lambda_runtime" {
  description = "Managed runtime for the worker Lambda."
  type        = string
  default     = "python3.11"
}

variable "lambda_timeout" {
  description = "Worker Lambda timeout in seconds."
  type        = number
  default     = 30
}

variable "lambda_memory_size" {
  description = "Worker Lambda memory size in MB."
  type        = number
  default     = 512
}

variable "lambda_reserved_concurrency" {
  description = "Reserved concurrent executions for the worker Lambda (bounds blast radius)."
  type        = number
  default     = 5
}

variable "lambda_batch_size" {
  description = "Number of SQS messages delivered to the worker per invocation."
  type        = number
  default     = 5
}

variable "max_inline_result_bytes" {
  description = "Results larger than this are written to S3 instead of being stored inline on the job item."
  type        = number
  default     = 8192
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the worker log group."
  type        = number
  default     = 365
}

variable "kms_deletion_window_in_days" {
  description = "Waiting period before the customer managed KMS key is deleted."
  type        = number
  default     = 30
}
