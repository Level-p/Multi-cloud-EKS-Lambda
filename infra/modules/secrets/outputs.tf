output "prefix" {
  value = var.name
}

output "read_policy_json" {
  description = "IAM policy per <service>-<environment>: read its own secrets and its environment's shared ones"
  value       = { for k, d in data.aws_iam_policy_document.read : k => d.json }
}

output "webhook_token_secret_arn" {
  description = "Alertmanager -> the production incident blog"
  value       = aws_secretsmanager_secret.generated["production/incidents/webhook-token"].arn
}

output "slack_webhook_secret_arn" {
  value = aws_secretsmanager_secret.slack_webhook.arn
}

output "mobile_gateway_secret" {
  description = "X-Mobile-Gateway: the mobile API Lambda sends it, the WAF exempts it from the per-IP rate limit"
  value       = random_password.this["platform/mobile-gateway-secret"].result
  sensitive   = true
}
