# ---------------------------------------------------------------------------
# Supply chain at admission (Kyverno), in the app namespaces only:
#   allowed-images   every container image is from our ECR (agripulse/*), the pinned oauth2-proxy, or the
#                    exact digests of Valkey and redis_exporter                                   (Deny)
#   signed-images    every agripulse/* image carries a Cosign signature made with the KMS key of infra/aws
#                    (var.enforce_image_signatures: Deny, or Audit to only report)
# Pod Security Admission "restricted" (main.tf) already rejects privileged and root containers there.
# Kyverno reads signatures from ECR with its Pod Identity role (ECR read only, infra/aws/main.tf).
# The policies are custom resources, so they come from a small local chart (./chart).
# ---------------------------------------------------------------------------

resource "helm_release" "kyverno" {
  name             = "kyverno"
  repository       = "https://kyverno.github.io/kyverno"
  chart            = "kyverno"
  version          = "3.9.2"
  namespace        = "kyverno"
  create_namespace = true
  timeout          = 600

  values = [yamlencode({
    admissionController = {
      replicas       = 2
      serviceMonitor = { enabled = true }
    }
    backgroundController = { serviceMonitor = { enabled = true } }
    cleanupController    = { serviceMonitor = { enabled = true } }
    reportsController    = { serviceMonitor = { enabled = true } }
  })]

  # ServiceMonitors need kube-prometheus-stack: the root makes this module wait for k8s-observability
}

locals {
  # Third-party images allowed in the app namespaces: EXACT references pinned by digest, the same strings as
  # redis.image and redis.exporterImage in gitops/charts/agripulse-web/values.yaml
  pinned_third_party = [
    "docker.io/valkey/valkey:9.2-alpine@sha256:cdcb7b1c7b6f1c9cb9442c71ac6275e78184657448407b2c1fdde7ee8024f4fb",
    "docker.io/oliver006/redis_exporter:v1.93.0@sha256:6ca518a72f30f7f576ef92963de1b91d3c3f5fd3aeac949f878bfda183587820",
  ]
}

resource "helm_release" "supply_chain_policies" {
  name      = "supply-chain-policies"
  chart     = "${path.module}/chart"
  namespace = "kyverno"

  values = [yamlencode({
    namespaces        = var.app_namespaces
    registry          = var.ecr_registry
    pinnedImages      = local.pinned_third_party
    cosignPublicKey   = var.cosign_public_key_pem
    signatureAction   = var.enforce_image_signatures ? "Deny" : "Audit"
    oauth2ProxyPrefix = "quay.io/oauth2-proxy/oauth2-proxy:v"
  })]

  depends_on = [helm_release.kyverno]
}
