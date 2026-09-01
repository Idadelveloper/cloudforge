output "aws_region" {
  description = "Region every resource in this stack was created in."
  value       = local.region
}

output "customers_table_name" {
  description = "DynamoDB table holding customer loyalty accounts and balances."
  value       = aws_dynamodb_table.customers.name
}

output "customers_table_arn" {
  description = "ARN of the customers table."
  value       = aws_dynamodb_table.customers.arn
}

output "transactions_table_name" {
  description = "DynamoDB table holding the point transaction ledger."
  value       = aws_dynamodb_table.transactions.name
}

output "transactions_table_arn" {
  description = "ARN of the transactions table."
  value       = aws_dynamodb_table.transactions.arn
}

output "idempotency_table_name" {
  description = "DynamoDB table holding purchase idempotency keys."
  value       = aws_dynamodb_table.idempotency.name
}

output "idempotency_table_arn" {
  description = "ARN of the idempotency table."
  value       = aws_dynamodb_table.idempotency.arn
}

output "idempotency_ttl_attribute" {
  description = "TTL attribute name configured on the idempotency table."
  value       = var.idempotency_ttl_attribute
}

output "purchases_queue_url" {
  description = "URL of the purchases queue consumed by the accrual worker."
  value       = aws_sqs_queue.purchases.url
}

output "purchases_queue_arn" {
  description = "ARN of the purchases queue."
  value       = aws_sqs_queue.purchases.arn
}

output "purchases_queue_name" {
  description = "Name of the purchases queue."
  value       = aws_sqs_queue.purchases.name
}

output "purchases_dlq_url" {
  description = "URL of the purchases dead-letter queue."
  value       = aws_sqs_queue.purchases_dlq.url
}

output "purchases_dlq_arn" {
  description = "ARN of the purchases dead-letter queue."
  value       = aws_sqs_queue.purchases_dlq.arn
}

output "gold_upgrades_topic_arn" {
  description = "SNS topic ARN used to announce gold-tier upgrades."
  value       = aws_sns_topic.gold_upgrades.arn
}

output "audit_bucket_name" {
  description = "S3 bucket where balance-change audit entries are written."
  value       = aws_s3_bucket.audit.bucket
}

output "audit_bucket_arn" {
  description = "ARN of the audit log bucket."
  value       = aws_s3_bucket.audit.arn
}

output "audit_log_prefix" {
  description = "Key prefix under which audit objects must be written."
  value       = var.audit_log_prefix
}

output "access_logs_bucket_name" {
  description = "S3 bucket receiving access logs for the audit bucket."
  value       = aws_s3_bucket.access_logs.bucket
}

output "log_group_name" {
  description = "CloudWatch log group for the loyalty points service."
  value       = aws_cloudwatch_log_group.service.name
}

output "dlq_alarm_name" {
  description = "CloudWatch alarm watching the purchases dead-letter queue."
  value       = aws_cloudwatch_metric_alarm.purchases_dlq_backlog.alarm_name
}

output "service_config_secret_name" {
  description = "Secrets Manager secret name holding the service runtime config."
  value       = aws_secretsmanager_secret.service_config.name
}

output "service_config_secret_arn" {
  description = "Secrets Manager secret ARN holding the service runtime config."
  value       = aws_secretsmanager_secret.service_config.arn
}

output "service_role_arn" {
  description = "ARN of the least-privilege role for the loyalty points service."
  value       = aws_iam_role.service.arn
}

output "service_role_name" {
  description = "Name of the least-privilege role for the loyalty points service."
  value       = aws_iam_role.service.name
}

output "gold_tier_threshold" {
  description = "Balance at which a customer is upgraded to gold tier."
  value       = var.gold_tier_threshold
}

output "account_id" {
  description = "Account the stack is deployed into."
  value       = local.account_id
}
