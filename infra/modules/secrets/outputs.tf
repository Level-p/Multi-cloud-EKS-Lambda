output "prefix" {
  value = var.name
}

output "read_policy_json" {
  description = "IAM policy per app (web, incidents): read its own secrets and the shared ones"
  value       = { for k, d in data.aws_iam_policy_document.read : k => d.json }
}

output "webhook_token_secret_arn" {
  value = aws_secretsmanager_secret.generated["incidents/webhook-token"].arn
}

output "slack_webhook_secret_arn" {
  value = aws_secretsmanager_secret.slack_webhook.arn
}

output "grafana_password_secret_arn" {
  value = aws_secretsmanager_secret.generated["platform/grafana-admin-password"].arn
}
