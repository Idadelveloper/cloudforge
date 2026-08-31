output "api_invoke_url" {
  description = "Base URL of the deployed notes API stage."
  value       = aws_api_gateway_stage.dev.invoke_url
}

output "api_id" {
  description = "Identifier of the REST API."
  value       = aws_api_gateway_rest_api.notes.id
}

output "api_stage_name" {
  description = "Deployed API Gateway stage name."
  value       = aws_api_gateway_stage.dev.stage_name
}

output "health_check_url" {
  description = "Full URL of the health endpoint used by smoke tests."
  value       = "${aws_api_gateway_stage.dev.invoke_url}/health"
}

output "notes_table_name" {
  description = "Name of the DynamoDB table storing notes."
  value       = aws_dynamodb_table.notes.name
}

output "notes_table_arn" {
  description = "ARN of the DynamoDB table storing notes."
  value       = aws_dynamodb_table.notes.arn
}

output "lambda_function_name" {
  description = "Name of the notes API Lambda function."
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

output "lambda_log_group_name" {
  description = "CloudWatch log group receiving Lambda logs."
  value       = aws_cloudwatch_log_group.lambda.name
}

output "api_log_group_name" {
  description = "CloudWatch log group receiving API Gateway access logs."
  value       = aws_cloudwatch_log_group.api.name
}

output "default_owner_id" {
  description = "Owner partition key used while the API is single tenant."
  value       = var.default_owner_id
}
