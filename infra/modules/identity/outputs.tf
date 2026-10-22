data "aws_region" "current" {}

output "farmers_user_pool_arn" {
  value = aws_cognito_user_pool.farmers.arn
}

output "farmers_user_pool_id" {
  value = aws_cognito_user_pool.farmers.id
}

output "mobile_client_id" {
  value = aws_cognito_user_pool_client.mobile.id
}

output "staff_user_pool_id" {
  value = aws_cognito_user_pool.staff.id
}

output "staff_issuer_url" {
  description = "OIDC issuer of the staff pool (oauth2-proxy --oidc-issuer-url)"
  value       = "https://${aws_cognito_user_pool.staff.endpoint}"
}

output "oauth2_client_id" {
  value = aws_cognito_user_pool_client.oauth2_proxy.id
}

output "oauth2_client_secret" {
  value     = aws_cognito_user_pool_client.oauth2_proxy.client_secret
  sensitive = true
}

output "staff_sign_in_domain" {
  value = "${aws_cognito_user_pool_domain.staff.domain}.auth.${data.aws_region.current.region}.amazoncognito.com"
}
