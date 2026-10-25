output "argocd_url" {
  description = "Argo CD through the ALB + WAF; user admin"
  value       = "https://${local.aws.hosts["argocd"]}"
}

output "argocd_password_command" {
  value = "kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d"
}

output "grafana_url" {
  description = "Grafana through the ALB + WAF; user admin"
  value       = "https://${local.aws.hosts["grafana"]}"
}

output "grafana_password_command" {
  value = "kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d"
}

output "image_signature_mode" {
  value = var.enforce_image_signatures ? "Deny (unsigned agripulse images are rejected)" : "Audit (reported in PolicyReports only)"
}
