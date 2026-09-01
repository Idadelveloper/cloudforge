output "aws_region" {
  description = "Region the telemetry resources were created in."
  value       = var.aws_region
}

output "devices_table_name" {
  description = "DynamoDB device registry table name (DEVICES_TABLE)."
  value       = aws_dynamodb_table.devices.name
}

output "devices_table_arn" {
  description = "ARN of the DynamoDB device registry table."
  value       = aws_dynamodb_table.devices.arn
}

output "readings_table_name" {
  description = "DynamoDB readings table name (READINGS_TABLE)."
  value       = aws_dynamodb_table.readings.name
}

output "readings_table_arn" {
  description = "ARN of the DynamoDB readings table."
  value       = aws_dynamodb_table.readings.arn
}

output "alerts_topic_arn" {
  description = "SNS topic ARN the ingest endpoint publishes threshold alerts to (ALERTS_TOPIC_ARN)."
  value       = aws_sns_topic.alerts.arn
}

output "alerts_topic_name" {
  description = "SNS alerts topic name."
  value       = aws_sns_topic.alerts.name
}

output "app_role_arn" {
  description = "ARN of the IAM role the backend service assumes."
  value       = aws_iam_role.app.arn
}

output "app_role_name" {
  description = "Name of the IAM role the backend service assumes."
  value       = aws_iam_role.app.name
}

output "app_policy_arn" {
  description = "ARN of the least-privilege policy attached to the application role."
  value       = aws_iam_policy.app.arn
}

output "log_group_name" {
  description = "CloudWatch log group name used by the service (LOG_GROUP_NAME)."
  value       = aws_cloudwatch_log_group.app.name
}

output "kms_key_arn" {
  description = "ARN of the CMK encrypting the tables, topic and log group."
  value       = aws_kms_key.telemetry.arn
}
