output "aws_region" {
  description = "Region the stack is deployed to."
  value       = var.aws_region
}

output "jobs_table_name" {
  description = "DynamoDB table holding job records."
  value       = aws_dynamodb_table.jobs.name
}

output "jobs_table_arn" {
  description = "ARN of the jobs DynamoDB table."
  value       = aws_dynamodb_table.jobs.arn
}

output "jobs_status_index_name" {
  description = "Global secondary index used for status-filtered job listing."
  value       = var.dynamodb_status_index_name
}

output "job_queue_url" {
  description = "URL of the SQS queue the API sends job messages to."
  value       = aws_sqs_queue.job_queue.id
}

output "job_queue_arn" {
  description = "ARN of the job queue."
  value       = aws_sqs_queue.job_queue.arn
}

output "job_queue_name" {
  description = "Name of the job queue."
  value       = aws_sqs_queue.job_queue.name
}

output "job_dlq_url" {
  description = "URL of the dead-letter queue inspected by GET /jobs/failed/dead-letter."
  value       = aws_sqs_queue.job_dlq.id
}

output "job_dlq_arn" {
  description = "ARN of the dead-letter queue."
  value       = aws_sqs_queue.job_dlq.arn
}

output "job_dlq_name" {
  description = "Name of the dead-letter queue."
  value       = aws_sqs_queue.job_dlq.name
}

output "max_receive_count" {
  description = "Number of receives before a message is redriven to the DLQ (2 = one retry)."
  value       = var.max_receive_count
}

output "results_bucket_name" {
  description = "S3 bucket storing large job result payloads."
  value       = aws_s3_bucket.results.bucket
}

output "results_bucket_arn" {
  description = "ARN of the job results bucket."
  value       = aws_s3_bucket.results.arn
}

output "results_object_prefix" {
  description = "Key prefix under which the worker stores result objects."
  value       = "results/"
}

output "access_logs_bucket_name" {
  description = "S3 bucket receiving access logs for the results bucket."
  value       = aws_s3_bucket.logs.bucket
}

output "worker_function_name" {
  description = "Name of the SQS-triggered worker Lambda."
  value       = aws_lambda_function.job_worker.function_name
}

output "worker_function_arn" {
  description = "ARN of the worker Lambda."
  value       = aws_lambda_function.job_worker.arn
}

output "worker_role_arn" {
  description = "Execution role used by the worker Lambda."
  value       = aws_iam_role.worker.arn
}

output "worker_log_group_name" {
  description = "CloudWatch log group for the worker Lambda."
  value       = aws_cloudwatch_log_group.worker.name
}

output "api_service_role_arn" {
  description = "Role the externally hosted FastAPI service assumes."
  value       = aws_iam_role.api.arn
}

output "api_service_policy_arn" {
  description = "Least-privilege policy attached to the API service role."
  value       = aws_iam_policy.api.arn
}

output "kms_key_arn" {
  description = "Customer managed KMS key protecting job data at rest."
  value       = aws_kms_key.main.arn
}

output "max_inline_result_bytes" {
  description = "Threshold above which results are written to S3 rather than DynamoDB."
  value       = var.max_inline_result_bytes
}
