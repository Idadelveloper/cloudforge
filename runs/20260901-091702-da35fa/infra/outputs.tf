output "aws_region" {
  description = "Region the order processing resources are deployed into."
  value       = var.aws_region
}

output "orders_table_name" {
  description = "Name of the DynamoDB orders table."
  value       = aws_dynamodb_table.orders.name
}

output "orders_table_arn" {
  description = "ARN of the DynamoDB orders table."
  value       = aws_dynamodb_table.orders.arn
}

output "orders_customer_index_name" {
  description = "Name of the GSI used to list orders by customer."
  value       = var.orders_customer_index_name
}

output "order_fulfillment_queue_url" {
  description = "URL of the SQS queue used for asynchronous order fulfilment."
  value       = aws_sqs_queue.order_fulfillment.id
}

output "order_fulfillment_queue_arn" {
  description = "ARN of the SQS fulfilment queue."
  value       = aws_sqs_queue.order_fulfillment.arn
}

output "order_fulfillment_queue_name" {
  description = "Name of the SQS fulfilment queue."
  value       = aws_sqs_queue.order_fulfillment.name
}

output "order_fulfillment_dlq_url" {
  description = "URL of the fulfilment dead-letter queue."
  value       = aws_sqs_queue.order_fulfillment_dlq.id
}

output "order_fulfillment_dlq_arn" {
  description = "ARN of the fulfilment dead-letter queue."
  value       = aws_sqs_queue.order_fulfillment_dlq.arn
}

output "order_status_topic_arn" {
  description = "ARN of the SNS topic publishing order status change events."
  value       = aws_sns_topic.order_status_changed.arn
}

output "order_status_topic_name" {
  description = "Name of the SNS order status topic."
  value       = aws_sns_topic.order_status_changed.name
}

output "order_fulfillment_worker_function_name" {
  description = "Name of the SQS triggered fulfilment worker Lambda."
  value       = aws_lambda_function.order_fulfillment_worker.function_name
}

output "order_fulfillment_worker_function_arn" {
  description = "ARN of the fulfilment worker Lambda."
  value       = aws_lambda_function.order_fulfillment_worker.arn
}

output "order_fulfillment_worker_role_arn" {
  description = "ARN of the fulfilment worker execution role."
  value       = aws_iam_role.order_fulfillment_worker.arn
}

output "order_api_service_policy_arn" {
  description = "ARN of the least privilege IAM policy for the FastAPI order service."
  value       = aws_iam_policy.order_api_service.arn
}

output "order_fulfillment_worker_log_group" {
  description = "CloudWatch log group holding fulfilment worker logs."
  value       = aws_cloudwatch_log_group.order_fulfillment_worker.name
}
