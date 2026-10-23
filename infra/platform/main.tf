# ---------------------------------------------------------------------------
# Platform inside EKS: composes the k8s-* modules in ../modules (Helm, installed once by Terraform; the apps
# themselves come from Git through Argo CD)
#   k8s-observability  kube-prometheus-stack (Prometheus, Alertmanager -> Slack + incident blog, Grafana), OpenTelemetry
#                      Collector, Tempo, Loki, YACE (CloudWatch metrics), ALL the alert rules, the gp3 StorageClass,
#                      the Grafana dashboard
#   k8s-controllers    AWS Load Balancer Controller (fills the ALB target groups), Secrets Store CSI (AWS)
#   k8s-policy         Kyverno: only our registry and pinned images, Cosign signatures checked at admission
#   k8s-gitops         app namespaces, Redis password, Argo CD + the two Applications, Argo Rollouts
#   k8s-edge           https://argocd.<zone> and https://grafana.<zone>: TargetGroupBindings to the edge's ALB
# Everything these modules need from AWS comes from the aws stack's state (provider.tf).
# ---------------------------------------------------------------------------

locals {
  app_namespaces = { agripulse-web = "agripulse", incident-blog = "incident-response" }
}

module "k8s_observability" {
  source                      = "../modules/k8s-observability"
  region                      = local.aws.region
  app_namespaces              = values(local.app_namespaces)
  webhook_token_secret_arn    = local.aws.webhook_token_secret_arn
  grafana_password_secret_arn = local.aws.grafana_password_secret_arn
  slack_webhook_secret_arn    = local.aws.slack_webhook_secret_arn
  grafana_host                = lookup(local.aws.hosts, "grafana", "grafana.missing-run-infra-aws-first")
}

module "k8s_controllers" {
  source       = "../modules/k8s-controllers"
  cluster_name = local.aws.cluster_name
  region       = local.aws.region
  vpc_id       = local.aws.vpc_id
  depends_on   = [module.k8s_observability] # ServiceMonitor CRD
}

module "k8s_policy" {
  source                   = "../modules/k8s-policy"
  app_namespaces           = values(local.app_namespaces)
  ecr_registry             = local.aws.ecr_registry
  cosign_public_key_pem    = local.aws.cosign_public_key_pem
  enforce_image_signatures = var.enforce_image_signatures
  depends_on               = [module.k8s_observability] # ServiceMonitor CRD
}

module "k8s_gitops" {
  source         = "../modules/k8s-gitops"
  app_namespaces = local.app_namespaces
  git_repo_url   = var.git_repo_url
  git_repo_token = var.git_repo_token
  git_revision   = var.git_revision
  argocd_host    = lookup(local.aws.hosts, "argocd", "argocd.missing-run-infra-aws-first")

  common_values = {
    image = { registry = local.aws.ecr_registry }
    aws = {
      region         = local.aws.region
      secretPrefix   = local.aws.secret_prefix
      albSourceCidrs = local.aws.public_subnet_cidrs # the ALB's nodes: the only source oauth2-proxy accepts
    }
    db   = { host = local.aws.db_endpoint, port = local.aws.db_port }
    auth = { issuerUrl = local.aws.staff_issuer_url, clientId = local.aws.oauth2_client_id }
  }
  app_values = {
    agripulse-web = {
      host        = local.aws.hosts["web"]
      priceAdmins = join(",", var.price_admins)
      aws         = { targetGroupArn = local.aws.target_group_arns["web"] }
      db = {
        name           = "agripulse"
        user           = local.aws.db_users["web"]
        masterSecretId = local.aws.db_master_secret_arn # the SQL bootstrap Job only
        users          = local.aws.db_users             # database => user it creates
      }
    }
    incident-blog = {
      host = local.aws.hosts["incidents"]
      aws  = { targetGroupArn = local.aws.target_group_arns["incidents"] }
      db   = { name = "incidents", user = local.aws.db_users["incidents"] }
    }
  }

  # Argo CD syncs the apps only once their TargetGroupBindings, secrets and admission policies can work
  depends_on = [module.k8s_controllers, module.k8s_policy, module.k8s_observability]
}

# Argo CD and Grafana behind the same CloudFront + WAF + ALB as the apps (their own sign-in)
module "k8s_edge" {
  source = "../modules/k8s-edge"
  bindings = [
    { name = "argocd", namespace = "argocd", service = "argocd-server", port = 80, targetGroupArn = lookup(local.aws.target_group_arns, "argocd", "") },
    { name = "grafana", namespace = "monitoring", service = "kube-prometheus-stack-grafana", port = 80, targetGroupArn = lookup(local.aws.target_group_arns, "grafana", "") },
  ]
  # the controller (the TargetGroupBinding kind), the argocd namespace and the Grafana Service must exist
  depends_on = [module.k8s_controllers, module.k8s_gitops, module.k8s_observability]
}
