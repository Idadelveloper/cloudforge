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
  value       = aws_lambda_function.handler.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Lambda function."
  value       = aws_lambda_function.handler.arn
}

output "api_gateway_id" {
  description = "ID of the API Gateway REST API."
  value       = aws_api_gateway_rest_api.api.id
}

output "api_invoke_url" {
  description = "Base invoke URL for the deployed API stage."
  value       = aws_api_gateway_stage.stage.invoke_url
}

output "lambda_role_arn" {
  description = "ARN of the Lambda execution IAM role."
  value       = aws_iam_role.lambda_role.arn
}
