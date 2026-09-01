output "devices_table_name" {
  description = "Name of the DynamoDB device registry table."
  value       = aws_dynamodb_table.devices.name
}

output "devices_table_arn" {
  description = "ARN of the DynamoDB device registry table."
  value       = aws_dynamodb_table.devices.arn
}

output "readings_table_name" {
  description = "Name of the DynamoDB telemetry readings table."
  value       = aws_dynamodb_table.readings.name
}

output "readings_table_arn" {
  description = "ARN of the DynamoDB telemetry readings table."
  value       = aws_dynamodb_table.readings.arn
}

output "readings_day_index_name" {
  description = "Local secondary index used for per-day reading lookups."
  value       = "device_day_index"
}

output "alerts_topic_arn" {
  description = "ARN of the SNS topic that receives threshold breach alerts."
  value       = aws_sns_topic.alerts.arn
}

output "alerts_topic_name" {
  description = "Name of the SNS alert topic."
  value       = aws_sns_topic.alerts.name
}

output "app_role_arn" {
  description = "ARN of the least-privilege IAM role for the backend service."
  value       = aws_iam_role.app.arn
}

output "app_role_name" {
  description = "Name of the least-privilege IAM role for the backend service."
  value       = aws_iam_role.app.name
}

output "app_policy_arn" {
  description = "ARN of the IAM policy attached to the backend service role."
  value       = aws_iam_policy.app.arn
}

output "log_group_name" {
  description = "CloudWatch log group for application logs."
  value       = aws_cloudwatch_log_group.app.name
}

output "kms_key_arn" {
  description = "ARN of the customer managed KMS key encrypting telemetry data at rest."
  value       = aws_kms_key.telemetry.arn
}

output "aws_region" {
  description = "Region the telemetry backend resources were created in."
  value       = var.aws_region
}
