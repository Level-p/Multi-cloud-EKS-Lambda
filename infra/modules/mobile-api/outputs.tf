output "invoke_domain" {
  description = "<api id>.execute-api.<region>.amazonaws.com: CloudFront's origin"
  value       = "${aws_api_gateway_rest_api.mobile.id}.execute-api.${data.aws_region.current.region}.amazonaws.com"
}

output "stage_name" {
  value = aws_api_gateway_stage.prod.stage_name
}

output "lambda_function_name" {
  value = aws_lambda_function.api.function_name
}

output "lambda_alias" {
  value = aws_lambda_alias.live.name
}
