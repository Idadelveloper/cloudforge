###############################################################################
# Provider / environment
###############################################################################

variable "aws_region" {
  description = "AWS region used for every resource in this stack."
  type        = string
  default     = "us-east-1"
}

variable "aws_endpoint_url" {
  description = "Endpoint used for all AWS service calls (LocalStack edge endpoint)."
  type        = string
  default     = "http://localhost:4566"
}

variable "aws_access_key" {
  description = "Access key id used against the LocalStack endpoint."
  type        = string
  default     = "test"
}

variable "aws_secret_key" {
  description = "Secret access key used against the LocalStack endpoint."
  type        = string
  default     = "test"
  sensitive   = true
}

variable "project_name" {
  description = "Project identifier applied as a default tag and used for the KMS alias."
  type        = string
  default     = "async-job-processor"
}

###############################################################################
# Resource names (defaults match the shared plan's aws_resources names)
###############################################################################

variable "jobs_table_name" {
  description = "DynamoDB table holding job records (plan resource: jobs)."
  type        = string
  default     = "jobs"
}

variable "jobs_status_index_name" {
  description = "GSI on the jobs table used by GET /jobs?status=."
  type        = string
  default     = "status-created_at-index"
}

variable "results_table_name" {
  description = "DynamoDB table holding job results (plan resource: job-results)."
  type        = string
  default     = "job-results"
}

variable "job_queue_name" {
  description = "SQS queue the API publishes jobs to (plan resource: job-queue)."
  type        = string
  default     = "job-queue"
}

variable "job_dlq_name" {
  description = "SQS dead-letter queue for exhausted jobs (plan resource: job-dlq)."
  type        = string
  default     = "job-dlq"
}

variable "results_bucket_name" {
  description = "S3 bucket holding oversized job results (plan resource: job-results-bucket)."
  type        = string
  default     = "job-results-bucket"
}

variable "access_logs_bucket_name" {
  description = "S3 bucket receiving server access logs for the results bucket."
  type        = string
  default     = "job-results-bucket-access-logs"
}

variable "worker_function_name" {
  description = "Lambda worker consuming the jobs queue (plan resource: job-worker)."
  type        = string
  default     = "job-worker"
}

variable "worker_role_name" {
  description = "IAM execution role for the Lambda worker (plan resource: job-worker-role)."
  type        = string
  default     = "job-worker-role"
}

variable "api_policy_name" {
  description = "IAM managed policy for the FastAPI service (plan resource: job-api-service-policy)."
  type        = string
  default     = "job-api-service-policy"
}

variable "alerts_topic_name" {
  description = "SNS topic for dead-letter notifications (plan resource: job-failure-alerts)."
  type        = string
  default     = "job-failure-alerts"
}

variable "api_secret_name" {
  description = "Secrets Manager secret holding the API submission token (plan resource: job-api-config)."
  type        = string
  default     = "job-api-config"
}

###############################################################################
# Behaviour tuning
###############################################################################

variable "job_max_attempts" {
  description = "maxReceiveCount for the redrive policy: one initial attempt plus one retry."
  type        = number
  default     = 2
}

variable "queue_visibility_timeout_seconds" {
  description = "Visibility timeout of the jobs queue (>= 3x the Lambda timeout)."
  type        = number
  default     = 180
}

variable "queue_message_retention_seconds" {
  description = "Retention for messages on the jobs queue."
  type        = number
  default     = 345600
}

variable "dlq_message_retention_seconds" {
  description = "Retention for dead-lettered messages (14 days)."
  type        = number
  default     = 1209600
}

variable "worker_runtime" {
  description = "Lambda runtime for the job worker."
  type        = string
  default     = "python3.11"
}

variable "worker_timeout_seconds" {
  description = "Lambda worker timeout."
  type        = number
  default     = 60
}

variable "worker_memory_mb" {
  description = "Lambda worker memory size."
  type        = number
  default     = 512
}

variable "worker_reserved_concurrency" {
  description = "Reserved concurrent executions for the job worker."
  type        = number
  default     = 5
}

variable "worker_batch_size" {
  description = "Number of SQS messages delivered per worker invocation."
  type        = number
  default     = 1
}

variable "max_inline_result_bytes" {
  description = "Results larger than this are offloaded to S3 instead of DynamoDB."
  type        = number
  default     = 300000
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the worker log group."
  type        = number
  default     = 365
}

variable "results_expiration_days" {
  description = "Lifecycle expiration for objects in the results bucket."
  type        = number
  default     = 365
}
