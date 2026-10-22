# Read by infra/platform (terraform_remote_state) and by the workflows
# (.github/actions/infra-outputs: every non-sensitive single-line string output becomes TF_OUT_<NAME>).

output "region" {
  value = var.region
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
  description = "Where the ALB's nodes live: the charts admit oauth2-proxy traffic from these only"
  value       = module.network.public_subnet_cidrs
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

output "db_users" {
  value = local.db_users
}

output "db_master_secret_arn" {
  value = module.database.master_secret_arn
}

# ---------------- edge and URLs ----------------
output "hosts" {
  value = module.edge.hosts
}

output "web_url" {
  value = "https://${local.hosts["web"]}"
}

output "incident_blog_url" {
  value = "https://${local.hosts["incidents"]}"
}

output "mobile_api_url" {
  value = "https://${local.hosts["api"]}"
}

output "media_cdn_url" {
  value = "https://${local.hosts["media"]}"
}

output "target_group_arns" {
  value = module.edge.target_group_arns
}

# ---------------- sign-in ----------------
output "staff_issuer_url" {
  value = module.identity.staff_issuer_url
}

output "oauth2_client_id" {
  value = module.identity.oauth2_client_id
}

output "staff_user_pool_id" {
  description = "Invite staff: aws cognito-idp admin-create-user --user-pool-id <this> --username you@example.com"
  value       = module.identity.staff_user_pool_id
}

output "farmers_user_pool_id" {
  value = module.identity.farmers_user_pool_id
}

output "mobile_client_id" {
  value = module.identity.mobile_client_id
}

# ---------------- mobile API (the pipeline's canary) ----------------
output "lambda_function_name" {
  value = module.mobile_api.lambda_function_name
}

output "lambda_alias" {
  value = module.mobile_api.lambda_alias
}

output "canary_alarm" {
  value = module.mobile_api.canary_alarm
}

# ---------------- secrets ----------------
output "secret_prefix" {
  value = module.secrets.prefix
}

output "webhook_token_secret_arn" {
  value = module.secrets.webhook_token_secret_arn
}

output "grafana_password_secret_arn" {
  value = module.secrets.grafana_password_secret_arn
}
