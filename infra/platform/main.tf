# ---------------------------------------------------------------------------
# Platform inside EKS: composes the k8s-* modules in ../modules (Helm, installed once by Terraform; the services
# themselves come from Git through Argo CD)
#   k8s-observability  kube-prometheus-stack (Prometheus, Alertmanager -> Slack + incident blog, Grafana), OpenTelemetry
#                      Collector, Tempo, Loki, YACE (CloudWatch metrics), ALL the alert rules, the gp3 StorageClass,
#                      the Grafana dashboard
#   k8s-controllers    AWS Load Balancer Controller (fills the ALB target groups), Secrets Store CSI (AWS)
#   k8s-policy         Kyverno: only our registry and pinned images, Cosign signatures checked at admission
#   k8s-database       the databases and IAM-authenticated users of every service in every environment
#   k8s-gitops         the 8 namespaces (<service>-<environment>), Redis passwords, Argo CD + one Application each,
#                      Argo Rollouts
#   k8s-edge           https://argocd.<zone> and https://grafana.<zone>: TargetGroupBindings to the edge's ALB
# Everything these modules need from AWS comes from the aws stack's state (provider.tf).
# ---------------------------------------------------------------------------

locals {
  deployments = local.aws.deployments # <service>-<environment> => { service, environment, host }
  url         = { for k, d in local.deployments : k => "https://${d.host}" }

  # Helm values only Terraform knows, per service and environment (merged over gitops/services/<service>/*.yaml)
  service_values = { for k, d in local.deployments : k => {
    service           = d.service
    environment       = d.environment
    requireBuiltImage = true # the charts' placeholder tag does not render in the cluster (no doomed first sync)
    image             = { registry = local.aws.ecr_registry }
    publicUrl         = local.url[k]
    urls              = { for s in local.aws.services : s => local.url["${s}-${d.environment}"] }
    cookie            = { domain = local.aws.env_domains[d.environment], name = "agripulse_session_${d.environment}" }
    jwt               = { issuer = "agripulse-accounts-${d.environment}" }
    db                = { host = local.aws.db_endpoint, port = local.aws.db_port, name = local.aws.databases[k].name, user = local.aws.databases[k].user }
    aws = {
      region         = local.aws.region
      targetGroupArn = local.aws.target_group_arns[k]
      albSourceCidrs = local.aws.public_subnet_cidrs # the ALB's nodes: the only outside source the pods accept
      secretPrefix   = "${local.aws.secret_prefix}/${d.environment}"
      secretRoot     = local.aws.secret_prefix
    }
    env = merge(
      { MEDIA_CDN_DOMAIN = local.aws.media_cdn_domain }, # the pages show listing photos from the CDN
      d.service == "farm" ? { MEDIA_BUCKET = local.aws.media_bucket } : {},
      d.service == "shop" ? { FARM_INTERNAL_URL = "http://farm.farm-${d.environment}.svc:8080" } : {},
    )
  } }
}

module "k8s_observability" {
  source                   = "../modules/k8s-observability"
  region                   = local.aws.region
  app_namespaces           = keys(local.deployments)
  webhook_token_secret_arn = local.aws.webhook_token_secret_arn
  slack_webhook_secret_arn = local.aws.slack_webhook_secret_arn
  grafana_host             = lookup(local.aws.hosts, "grafana", "grafana.missing-run-infra-aws-first")
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
  app_namespaces           = keys(local.deployments)
  ecr_registry             = local.aws.ecr_registry
  cosign_public_key_pem    = local.aws.cosign_public_key_pem
  enforce_image_signatures = var.enforce_image_signatures
  depends_on               = [module.k8s_observability] # ServiceMonitor CRD
}

# The databases exist before Argo CD starts any service (Terraform waits for the Job)
module "k8s_database" {
  source            = "../modules/k8s-database"
  databases         = local.aws.databases
  db_host           = local.aws.db_endpoint
  db_port           = local.aws.db_port
  master_secret_arn = local.aws.db_master_secret_arn
  region            = local.aws.region
  depends_on        = [module.k8s_controllers] # the Secrets Store CSI driver reads the master secret
}

module "k8s_gitops" {
  source            = "../modules/k8s-gitops"
  deployments       = { for k, d in local.deployments : k => { service = d.service, environment = d.environment, values = local.service_values[k] } }
  redis_deployments = [for k, d in local.deployments : k if d.service == "shop"]
  git_repo_url      = var.git_repo_url
  git_repo_token    = var.git_repo_token
  git_revision      = var.git_revision
  argocd_host       = lookup(local.aws.hosts, "argocd", "argocd.missing-run-infra-aws-first")

  # Argo CD syncs the services only once their TargetGroupBindings, secrets, admission policies and databases can work
  depends_on = [module.k8s_controllers, module.k8s_policy, module.k8s_observability, module.k8s_database]
}

# Argo CD and Grafana behind the same ALB + WAF as the services (their own sign-in)
module "k8s_edge" {
  source = "../modules/k8s-edge"
  bindings = [
    { name = "argocd", namespace = "argocd", service = "argocd-server", port = 80, targetGroupArn = lookup(local.aws.target_group_arns, "argocd", "") },
    { name = "grafana", namespace = "monitoring", service = "kube-prometheus-stack-grafana", port = 80, targetGroupArn = lookup(local.aws.target_group_arns, "grafana", "") },
  ]
  # the controller (the TargetGroupBinding kind), the argocd namespace and the Grafana Service must exist
  depends_on = [module.k8s_controllers, module.k8s_gitops, module.k8s_observability]
}
