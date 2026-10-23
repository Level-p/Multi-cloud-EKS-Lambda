output "argocd_url" {
  description = "Argo CD through CloudFront + WAF; user admin"
  value       = "https://${local.aws.hosts["argocd"]}"
}

output "argocd_password_command" {
  value = "kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d"
}

output "grafana_url" {
  description = "Grafana through CloudFront + WAF; user admin"
  value       = "https://${local.aws.hosts["grafana"]}"
}

output "grafana_password_command" {
  value = "aws secretsmanager get-secret-value --secret-id ${local.aws.secret_prefix}/platform/grafana-admin-password --query SecretString --output text"
}

output "image_signature_mode" {
  value = var.enforce_image_signatures ? "Deny (unsigned agripulse images are rejected)" : "Audit (reported in PolicyReports only)"
}
