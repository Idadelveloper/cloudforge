output "notes_table_name" {
  description = "Name of the DynamoDB table storing notes."
  value       = aws_dynamodb_table.notes.name
}

output "notes_table_arn" {
  description = "ARN of the DynamoDB notes table."
  value       = aws_dynamodb_table.notes.arn
}

output "lambda_function_name" {
  description = "Name of the Lambda function hosting the notes API."
  value       = aws_lambda_function.notes_api.function_name
}

output "lambda_function_arn" {
  description = "ARN of the notes API Lambda function."
  value       = aws_lambda_function.notes_api.arn
}

output "lambda_role_arn" {
  description = "ARN of the Lambda execution role."
  value       = aws_iam_role.lambda.arn
}

output "api_gateway_id" {
  description = "Identifier of the REST API."
  value       = aws_api_gateway_rest_api.notes_api.id
}

output "api_base_url" {
  description = "Invoke URL of the deployed REST API stage."
  value       = aws_api_gateway_stage.notes_api.invoke_url
}

output "api_stage_name" {
  description = "Deployed API Gateway stage name."
  value       = aws_api_gateway_stage.notes_api.stage_name
}

output "lambda_log_group_name" {
  description = "CloudWatch log group receiving Lambda logs."
  value       = aws_cloudwatch_log_group.lambda.name
}

output "api_gateway_log_group_name" {
  description = "CloudWatch log group receiving API Gateway access logs."
  value       = aws_cloudwatch_log_group.api_gateway.name
}

output "kms_key_arn" {
  description = "ARN of the customer managed key encrypting notes data and logs."
  value       = aws_kms_key.notes.arn
}
