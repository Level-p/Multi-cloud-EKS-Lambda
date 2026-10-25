# ---------------------------------------------------------------------------
# GitOps: Argo CD syncs every service into its own namespace, in staging and in production, from this repository:
#   gitops/charts/agripulse-service           the one chart all services use
#   gitops/services/<service>/values.yaml     what the service is (secrets, settings, Redis, alerts)
#   gitops/services/<service>/<env>.yaml      the image of that environment (the pipelines write only this)
#   + valuesObject below                      what only Terraform knows (hosts, target group, database, URLs)
# Argo Rollouts runs every service's canary with automated Prometheus analysis (the AIOps deployment gate).
# Also here: the 8 namespaces (Pod Security "restricted") and the Redis passwords of the shop's caches.
# ---------------------------------------------------------------------------

resource "kubernetes_namespace_v1" "apps" {
  for_each = var.deployments
  metadata {
    name = each.key
    labels = {
      "app.kubernetes.io/part-of"          = "agripulse"
      "agripulse.io/service"               = each.value.service
      "agripulse.io/environment"           = each.value.environment
      "pod-security.kubernetes.io/enforce" = "restricted"
      "pod-security.kubernetes.io/audit"   = "restricted"
    }
  }
}

# ---------------- Redis caches (Valkey) inside EKS: their passwords ----------------
# Services with redis.enabled (the shop) run Valkey in their own namespace (gitops/charts/agripulse-service/
# templates/redis.yaml). Terraform creates its password as a Kubernetes Secret "<service>-redis"; never in Git.
resource "random_password" "redis" {
  for_each = toset(var.redis_deployments)
  length   = 40
  special  = false
}

resource "kubernetes_secret_v1" "redis" {
  for_each = toset(var.redis_deployments)
  metadata {
    name      = "${var.deployments[each.key].service}-redis"
    namespace = kubernetes_namespace_v1.apps[each.key].metadata[0].name
  }
  data = { password = random_password.redis[each.key].result }
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
    global  = { domain = var.argocd_host }              # https://argocd.<zone> (links, redirects)
    configs = { params = { "server.insecure" = true } } # TLS ends at the ALB; plain HTTP to the pod
    server  = { service = { type = "ClusterIP" } }      # public only through the edge (modules/k8s-edge)
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
        description                = "AgriPulse microservices, staging and production"
        sourceRepos                = [var.git_repo_url]
        destinations               = [for ns in keys(var.deployments) : { server = "https://kubernetes.default.svc", namespace = ns }]
        clusterResourceWhitelist   = []
        namespaceResourceWhitelist = [{ group = "*", kind = "*" }]
      }
    }
    # One Application per service and environment, named like its namespace: shop-staging, shop-production...
    applications = { for k, d in var.deployments : k => {
      namespace  = "argocd"
      project    = "agripulse"
      finalizers = ["resources-finalizer.argocd.argoproj.io"]
      source = {
        repoURL        = var.git_repo_url
        targetRevision = var.git_revision
        path           = "gitops/charts/agripulse-service"
        helm = {
          valueFiles   = ["../../services/${d.service}/values.yaml", "../../services/${d.service}/${d.environment}.yaml"]
          valuesObject = d.values
        }
      }
      destination = { server = "https://kubernetes.default.svc", namespace = k }
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
