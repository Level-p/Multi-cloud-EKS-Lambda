output "argocd_port_forward" {
  value = "kubectl port-forward -n argocd svc/argocd-server 8080:80   # admin password: kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d"
}

output "grafana_access" {
  description = "Grafana is internal to the cluster: port-forward, then sign in as admin"
  value = {
    port_forward = "kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80   # http://localhost:3000"
    password     = "aws secretsmanager get-secret-value --secret-id ${local.aws.secret_prefix}/platform/grafana-admin-password --query SecretString --output text"
  }
}

output "image_signature_mode" {
  value = var.enforce_image_signatures ? "Deny (unsigned agripulse images are rejected)" : "Audit (reported in PolicyReports only)"
}
