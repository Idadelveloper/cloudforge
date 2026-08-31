output "dynamodb_table_name" {
  description = "Name of the DynamoDB notes table."
  value       = aws_dynamodb_table.notes.name
}

output "dynamodb_table_arn" {
  description = "ARN of the DynamoDB notes table."
  value       = aws_dynamodb_table.notes.arn
}

output "lambda_function_name" {
  description = "Name of the Lambda function."
  value       = aws_lambda_function.api.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Lambda function."
  value       = aws_lambda_function.api.arn
}

output "iam_role_arn" {
  description = "ARN of the Lambda execution role."
  value       = aws_iam_role.lambda.arn
}

output "lambda_dlq_url" {
  description = "URL of the Lambda dead-letter queue."
  value       = aws_sqs_queue.lambda_dlq.url
}

output "api_id" {
  description = "ID of the API Gateway REST API."
  value       = aws_api_gateway_rest_api.api.id
}

output "api_invoke_url" {
  description = "Base invoke URL for the deployed API stage."
  value       = "https://${aws_api_gateway_rest_api.api.id}.execute-api.${data.aws_region.current.name}.amazonaws.com/${aws_api_gateway_stage.api.stage_name}"
}

output "kms_key_arn" {
  description = "ARN of the KMS key used for encryption."
  value       = aws_kms_key.main.arn
}
