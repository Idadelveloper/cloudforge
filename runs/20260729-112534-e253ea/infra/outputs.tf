output "api_endpoint" {
  description = "Base invoke URL for the URL shortener API."
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "dynamodb_table_name" {
  description = "Name of the DynamoDB table storing URL mappings."
  value       = aws_dynamodb_table.url_mappings.name
}

output "dynamodb_table_arn" {
  description = "ARN of the DynamoDB table storing URL mappings."
  value       = aws_dynamodb_table.url_mappings.arn
}

output "lambda_function_name" {
  description = "Name of the Lambda function."
  value       = aws_lambda_function.fn.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Lambda function."
  value       = aws_lambda_function.fn.arn
}

output "lambda_role_arn" {
  description = "ARN of the IAM role assumed by the Lambda function."
  value       = aws_iam_role.lambda.arn
}

output "kms_key_arn" {
  description = "ARN of the KMS key used for encryption."
  value       = aws_kms_key.main.arn
}
