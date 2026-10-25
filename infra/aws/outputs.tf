# Read by infra/platform (terraform_remote_state) and by the workflows
# (.github/actions/infra-outputs: every non-sensitive single-line string output becomes TF_OUT_<NAME>).

output "region" {
  value = var.region
}

output "name" {
  value = var.name
}

# ---------------- EKS ----------------
output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "cluster_ca" {
  value = module.eks.cluster_ca
}

output "kubeconfig_command" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "vpc_id" {
  value = module.network.vpc_id
}

output "public_subnet_cidrs" {
  description = "Where the ALB's nodes live: the services' network policies admit outside traffic from these only"
  value       = module.network.public_subnet_cidrs
}

# ---------------- the services ----------------
output "services" {
  value = local.services
}

output "environments" {
  value = local.environments
}

output "deployments" {
  description = "<service>-<environment> => { service, environment, host }: one namespace and Argo CD Application each"
  value       = local.deployments
}

output "env_domains" {
  description = "environment => parent domain (cookie domain): staging.<zone>, <zone>"
  value       = local.env_domain
}

# ---------------- supply chain ----------------
output "ecr_registry" {
  value = module.registry.ecr_registry
}

output "cosign_key_uri" {
  value = module.registry.cosign_key_uri
}

output "cosign_public_key_pem" {
  value = module.registry.cosign_public_key_pem
}

# ---------------- database ----------------
output "db_endpoint" {
  value = module.database.endpoint
}

output "db_port" {
  value = module.database.port
}

output "databases" {
  description = "<service>-<environment> => { name, user }: created by infra/platform (the db-bootstrap Job)"
  value       = local.databases
}

output "db_master_secret_arn" {
  value = module.database.master_secret_arn
}

# ---------------- edge and URLs ----------------
output "hosts" {
  value = module.edge.hosts
}

output "target_group_arns" {
  description = "<service>-<environment> (and argocd, grafana) => its ALB target group"
  value       = module.edge.target_group_arns
}

output "media_bucket" {
  value = module.storage.bucket
}

output "media_cdn_domain" {
  value = local.hosts["media"]
}

# One string output per URL: the workflows read them as TF_OUT_<NAME> (DAST of the public URLs)
output "accounts_url" {
  value = "https://${local.hosts["accounts-production"]}"
}

output "farm_url" {
  value = "https://${local.hosts["farm-production"]}"
}

output "shop_url" {
  value = "https://${local.hosts["shop-production"]}"
}

output "incidents_url" {
  value = "https://${local.hosts["incidents-production"]}"
}

output "accounts_staging_url" {
  value = "https://${local.hosts["accounts-staging"]}"
}

output "farm_staging_url" {
  value = "https://${local.hosts["farm-staging"]}"
}

output "shop_staging_url" {
  value = "https://${local.hosts["shop-staging"]}"
}

output "incidents_staging_url" {
  value = "https://${local.hosts["incidents-staging"]}"
}

output "mobile_api_url" {
  value = "https://${local.hosts["api"]}"
}

output "media_cdn_url" {
  value = "https://${local.hosts["media"]}"
}

# ---------------- mobile API (the pipeline's canary) ----------------
output "lambda_function_name" {
  value = module.mobile_api.lambda_function_name
}

output "lambda_alias" {
  value = module.mobile_api.lambda_alias
}

# ---------------- secrets ----------------
output "secret_prefix" {
  description = "Secrets Manager names: <prefix>/<environment>/... and <prefix>/platform/..."
  value       = module.secrets.prefix
}

output "webhook_token_secret_arn" {
  value = module.secrets.webhook_token_secret_arn
}

output "slack_webhook_secret_arn" {
  description = "The Slack webhook every alert and incident report goes to"
  value       = module.secrets.slack_webhook_secret_arn
}

output "grafana_password_secret_arn" {
  value = module.secrets.grafana_password_secret_arn
}
