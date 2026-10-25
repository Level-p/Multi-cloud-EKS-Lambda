output "rest_api_id" {
  value = aws_api_gateway_rest_api.mobile.id
}

output "stage_name" {
  value = aws_api_gateway_stage.prod.stage_name
}

output "stage_arn" {
  description = "The WAF is attached to it (modules/edge)"
  value       = aws_api_gateway_stage.prod.arn
}

output "lambda_function_name" {
  value = aws_lambda_function.api.function_name
}

output "lambda_alias" {
  value = aws_lambda_alias.live.name
}
