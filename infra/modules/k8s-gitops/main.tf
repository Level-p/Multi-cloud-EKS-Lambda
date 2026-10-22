# ---------------------------------------------------------------------------
# GitOps: Argo CD syncs gitops/charts/<app> from this repository into EKS; the pipelines only commit image
# tags. Argo Rollouts runs the web app's canary with automated Prometheus analysis (AIOps deployment gate).
# Also here: the app namespaces (Pod Security "restricted") and the Redis cache's password.
# ---------------------------------------------------------------------------

resource "kubernetes_namespace_v1" "apps" {
  for_each = toset(values(var.app_namespaces))
  metadata {
    name = each.value
    labels = {
      "app.kubernetes.io/part-of"          = "agripulse"
      "pod-security.kubernetes.io/enforce" = "restricted"
      "pod-security.kubernetes.io/audit"   = "restricted"
    }
  }
}

# ---------------- Redis cache (Valkey) inside EKS: its password ----------------
# The cache runs in the cluster (gitops/charts/agripulse-web/templates/redis.yaml). Terraform creates its
# password as a Kubernetes Secret, read by Valkey and by the web pods; never in Git.
resource "random_password" "redis" {
  length  = 40
  special = false
}

resource "kubernetes_secret_v1" "redis" {
  metadata {
    name      = "agripulse-redis"
    namespace = kubernetes_namespace_v1.apps["agripulse"].metadata[0].name
  }
  data = { password = random_password.redis.result }
}

# ---------------- progressive delivery ----------------
resource "helm_release" "argo_rollouts" {
  name             = "argo-rollouts"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-rollouts"
  version          = "2.43.8"
  namespace        = "argo-rollouts"
  create_namespace = true
  values           = [yamlencode({ controller = { metrics = { enabled = true, serviceMonitor = { enabled = true } } } })]
}

# ---------------- Argo CD ----------------
resource "helm_release" "argocd" {
  name             = "argocd"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  version          = "10.10.5"
  namespace        = "argocd"
  create_namespace = true
  values = [yamlencode({
    configs = { params = { "server.insecure" = true } }
    server  = { service = { type = "ClusterIP" } } # reach it with kubectl port-forward (EKS access entries apply)
  })]
}

resource "kubernetes_secret_v1" "repo" {
  count = nonsensitive(var.git_repo_token == "") ? 0 : 1
  metadata {
    name      = "agripulse-repo"
    namespace = "argocd"
    labels    = { "argocd.argoproj.io/secret-type" = "repository" }
  }
  data       = { type = "git", url = var.git_repo_url, username = "argocd", password = var.git_repo_token }
  depends_on = [helm_release.argocd]
}

locals {
  # Merged over each chart's values.yaml by Helm: values only Terraform knows (the pipeline changes image.tag only)
  app_values = {
    agripulse-web = merge(var.common_values, var.app_values["agripulse-web"], {
      aws = merge(var.common_values.aws, var.app_values["agripulse-web"].aws)
      db  = merge(var.common_values.db, var.app_values["agripulse-web"].db)
    })
    incident-blog = merge(var.common_values, var.app_values["incident-blog"], {
      aws = merge(var.common_values.aws, var.app_values["incident-blog"].aws)
      db  = merge(var.common_values.db, var.app_values["incident-blog"].db)
    })
  }
}

resource "helm_release" "argocd_apps" {
  name       = "argocd-apps"
  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argocd-apps"
  version    = "2.0.7"
  namespace  = "argocd"

  values = [yamlencode({
    projects = {
      agripulse = {
        namespace                  = "argocd"
        description                = "AgriPulse web and incident blog"
        sourceRepos                = [var.git_repo_url]
        destinations               = [for ns in values(var.app_namespaces) : { server = "https://kubernetes.default.svc", namespace = ns }]
        clusterResourceWhitelist   = []
        namespaceResourceWhitelist = [{ group = "*", kind = "*" }]
      }
    }
    applications = { for app, ns in var.app_namespaces : app => {
      namespace  = "argocd"
      project    = "agripulse"
      finalizers = ["resources-finalizer.argocd.argoproj.io"]
      source = {
        repoURL        = var.git_repo_url
        targetRevision = var.git_revision
        path           = "gitops/charts/${app}"
        helm           = { valueFiles = ["values.yaml"], valuesObject = local.app_values[app] }
      }
      destination = { server = "https://kubernetes.default.svc", namespace = ns }
      syncPolicy = {
        automated   = { prune = true, selfHeal = true }
        syncOptions = ["CreateNamespace=false"]
        retry       = { limit = 5, backoff = { duration = "10s", factor = 2, maxDuration = "3m" } }
      }
    } }
  })]

  depends_on = [helm_release.argocd, kubernetes_secret_v1.repo, kubernetes_namespace_v1.apps, helm_release.argo_rollouts,
  kubernetes_secret_v1.redis]
}
