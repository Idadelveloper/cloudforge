output "api_gateway_id" {
  description = "Identifier of the notes REST API."
  value       = aws_api_gateway_rest_api.notes.id
}

output "api_stage_name" {
  description = "Deployed API Gateway stage name."
  value       = aws_api_gateway_stage.notes.stage_name
}

output "api_invoke_url" {
  description = "AWS invoke URL for the notes API."
  value       = aws_api_gateway_stage.notes.invoke_url
}

output "api_localstack_url" {
  description = "LocalStack-compatible base URL for the notes API."
  value       = "http://localhost:4566/restapis/${aws_api_gateway_rest_api.notes.id}/${aws_api_gateway_stage.notes.stage_name}/_user_request_"
}

output "dynamodb_table_name" {
  description = "Name of the DynamoDB table storing notes."
  value       = aws_dynamodb_table.notes.name
}

output "dynamodb_table_arn" {
  description = "ARN of the DynamoDB table storing notes."
  value       = aws_dynamodb_table.notes.arn
}

output "lambda_function_name" {
  description = "Name of the Lambda function hosting the notes API."
  value       = aws_lambda_function.notes_api.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Lambda function hosting the notes API."
  value       = aws_lambda_function.notes_api.arn
}

output "lambda_role_arn" {
  description = "ARN of the Lambda execution role."
  value       = aws_iam_role.lambda.arn
}

output "lambda_log_group_name" {
  description = "CloudWatch log group receiving Lambda logs."
  value       = aws_cloudwatch_log_group.lambda.name
}

output "api_access_log_group_name" {
  description = "CloudWatch log group receiving API Gateway access logs."
  value       = aws_cloudwatch_log_group.api_access.name
}

output "kms_key_arn" {
  description = "ARN of the customer managed key used for encryption at rest."
  value       = aws_kms_key.notes.arn
}
