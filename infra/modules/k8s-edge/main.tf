# ---------------------------------------------------------------------------
# Public URLs of the platform tools, through the same edge as the apps:
#   https://argocd.<zone>  -> Route 53 -> ALB + AWS WAF -> argocd-server
#   https://grafana.<zone> -> Route 53 -> ALB + AWS WAF -> Grafana
# Terraform's aws stack owns the target groups (modules/edge); these TargetGroupBindings ask the AWS Load
# Balancer Controller to keep them filled with the pod IPs. They come from a small local chart: the
# TargetGroupBinding kind exists only once the controller is installed, which Helm (unlike a plan) can wait for.
# ---------------------------------------------------------------------------

resource "helm_release" "target_group_bindings" {
  name      = "platform-edge"
  chart     = "${path.module}/chart"
  namespace = "kube-system"

  values = [yamlencode({ bindings = var.bindings })]
}
