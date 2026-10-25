# ---------------------------------------------------------------------------
# AgriPulse on AWS (eu-west-2): composes the modules in ../modules
#
#   network      VPC: public (ALB, NAT), private (EKS), database (RDS) subnets in two zones
#   eks          the cluster: the four microservices in staging and production namespaces, Redis, observability
#   database     ONE RDS PostgreSQL db.t3.micro, Multi-AZ, IAM authentication; one database per service and environment
#   registry     ECR repositories (one per service) + the Cosign signing key (KMS)
#   storage      S3 media bucket (listing photos) + GuardDuty malware scanning
#   secrets      app secrets in Secrets Manager, one set per environment (token keys, CSRF, internal tokens)
#   mobile-api   API Gateway + Lambda: the mobile app's gateway to the production services
#   edge         ONE WAF web ACL on three CloudFront distributions (services, mobile API, images) + ALB + DNS
#   monitoring   AWS events Prometheus cannot see (GuardDuty malware, RDS failover) -> EventBridge -> Slack.
#                Every other alert is a Prometheus rule in EKS (OpenTelemetry + CloudWatch via YACE) -> Slack
# In-cluster software (controllers, observability, Argo CD, Kyverno, the databases' users) is the second stack: ../platform.
# ---------------------------------------------------------------------------

locals {
  cluster_name = "${var.name}-eks"
  zone         = var.dns_zone_name

  # THE microservices and environments. Every service runs in both, each in its own namespace (<service>-<environment>),
  # with its own host, database, PostgreSQL user, AWS role, secrets and ALB target group, all derived from here.
  services     = ["accounts", "farm", "shop", "incidents"]
  environments = ["staging", "production"]
  env_domain   = { production = local.zone, staging = "staging.${local.zone}" }
  deployments = { for p in setproduct(local.services, local.environments) :
  "${p[0]}-${p[1]}" => { service = p[0], environment = p[1], host = "${p[0]}.${local.env_domain[p[1]]}" } }

  databases = { for k, d in local.deployments : k => { name = "${d.service}_${d.environment}", user = "${var.name}_${d.service}_${d.environment}" } }
  db_connect_arn = { for k, d in local.databases :
  k => "arn:aws:rds-db:${var.region}:${data.aws_caller_identity.current.account_id}:dbuser:${module.database.resource_id}/${d.user}" }

  # ALB rules: the priority of a rule is fixed (a changed priority replaces the rule), so new hosts go at the END
  route_order = concat(flatten([for e in reverse(local.environments) : [for s in local.services : "${s}-${e}"]]), ["argocd", "grafana"])
  tool_routes = {
    argocd  = { host = "argocd.${local.zone}", port = 8080, health = "/healthz" } # argocd-server
    grafana = { host = "grafana.${local.zone}", port = 3000, health = "/api/health" }
  }
  alb_routes = { for i, k in local.route_order : k => merge(
    contains(keys(local.tool_routes), k) ? local.tool_routes[k] : { host = local.deployments[k].host, port = 8080, health = "/healthz" },
  { priority = 10 + i }) }
  hosts = merge({ for k, r in local.alb_routes : k => r.host }, { api = "api.${local.zone}", media = "media.${local.zone}" })
}

data "aws_caller_identity" "current" {}

# CloudFront sends this header to the ALB and API Gateway; they refuse requests without it
resource "random_password" "origin_verify" {
  length  = 48
  special = false
}

module "network" {
  source       = "../modules/network"
  name         = var.name
  region       = var.region
  cluster_name = local.cluster_name
  az_count     = var.az_count
}

# ---------------- compute: EKS ----------------
module "eks" {
  source              = "../modules/eks"
  cluster_name        = local.cluster_name
  kubernetes_version  = var.kubernetes_version
  private_subnet_ids  = module.network.private_subnet_ids
  node_instance_types = var.node_instance_types
  node_desired        = var.node_desired
  admin_arns          = var.cluster_admin_arns

  # AWS permissions per Kubernetes ServiceAccount (EKS Pod Identity)
  pod_identities = merge(
    # Each service in each environment: its own secrets, its own PostgreSQL user; the farm service also signs
    # photo uploads (a pre-signed POST carries the signer's rights: uploads/ only)
    { for k, d in local.deployments : k => {
      namespace = k, service_account = d.service, managed_policy_arns = []
      inline_policy = jsonencode({
        Version = "2012-10-17"
        Statement = concat(jsondecode(module.secrets.read_policy_json[k]).Statement,
          [{ Effect = "Allow", Action = ["rds-db:connect"], Resource = local.db_connect_arn[k] }],
          d.service == "farm" ? [{ Effect = "Allow", Action = ["s3:PutObject"], Resource = "${module.storage.bucket_arn}/uploads/*" }] : [],
        )
      })
    } },
    {
      # Creates the databases and their IAM-authenticated users with the master password (infra/modules/k8s-database):
      # the only workload that can read the master secret
      db-bootstrap = {
        namespace = "db-bootstrap", service_account = "db-bootstrap", managed_policy_arns = []
        inline_policy = jsonencode({
          Version   = "2012-10-17"
          Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = module.database.master_secret_arn }]
        })
      }
      # YACE: brings the CloudWatch metrics of the ALB, API Gateway, Lambda and RDS into Prometheus, where the alert
      # rules run (read-only: metrics and resource tags)
      yace = {
        namespace = "monitoring", service_account = "yace", managed_policy_arns = []
        inline_policy = jsonencode({
          Version = "2012-10-17"
          Statement = [{ Effect = "Allow", Resource = "*", Action = ["cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics",
          "cloudwatch:ListMetrics", "tag:GetResources", "apigateway:GET"] }]
        })
      }
      # Reads Cosign signatures from ECR for the admission checks
      kyverno = {
        namespace = "kyverno", service_account = "kyverno-admission-controller", managed_policy_arns = []
        inline_policy = jsonencode({
          Version = "2012-10-17"
          Statement = [{ Effect = "Allow", Resource = "*", Action = ["ecr:GetAuthorizationToken", "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages", "ecr:ListImages"] }]
        })
      }
    },
  )
}

# ---------------- data ----------------
module "database" {
  source                    = "../modules/database"
  name                      = var.name
  vpc_id                    = module.network.vpc_id
  subnet_ids                = module.network.database_subnet_ids
  instance_class            = var.db_instance_class
  deletion_protection       = var.deletion_protection
  client_security_group_ids = { eks-pods = module.eks.cluster_security_group_id }
}

module "registry" {
  source              = "../modules/registry"
  name                = var.name
  repositories        = local.services
  deletion_protection = var.deletion_protection
}

module "storage" {
  source              = "../modules/storage"
  name                = var.name
  region              = var.region
  allowed_origins     = [for e in local.environments : "https://${local.deployments["farm-${e}"].host}"]
  deletion_protection = var.deletion_protection
}

module "secrets" {
  source            = "../modules/secrets"
  name              = var.name
  region            = var.region
  services          = local.services
  environments      = local.environments
  slack_webhook_url = var.slack_webhook_url
}

# ---------------- mobile API ----------------
module "mobile_api" {
  source                = "../modules/mobile-api"
  name                  = var.name
  service_urls          = { for s in ["accounts", "farm", "shop"] : s => "https://${local.deployments["${s}-production"].host}" }
  origin_verify_secret  = random_password.origin_verify.result
  mobile_gateway_secret = module.secrets.mobile_gateway_secret
}

# ---------------- edge: one WAF, three CloudFront distributions ----------------
module "edge" {
  source = "../modules/edge"
  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }
  name                              = var.name
  dns_zone_name                     = var.dns_zone_name
  alb_routes                        = local.alb_routes
  api_host                          = local.hosts["api"]
  media_host                        = local.hosts["media"]
  vpc_id                            = module.network.vpc_id
  public_subnet_ids                 = module.network.public_subnet_ids
  cluster_security_group_id         = module.eks.cluster_security_group_id
  media_bucket                      = module.storage.bucket
  media_bucket_arn                  = module.storage.bucket_arn
  media_bucket_regional_domain_name = module.storage.bucket_regional_domain_name
  api_invoke_domain                 = module.mobile_api.invoke_domain
  api_stage_name                    = module.mobile_api.stage_name
  origin_verify_secret              = random_password.origin_verify.result
  mobile_gateway_secret             = module.secrets.mobile_gateway_secret
  waf_rate_limit                    = var.waf_rate_limit
  deletion_protection               = var.deletion_protection
}

module "monitoring" {
  source            = "../modules/monitoring"
  name              = var.name
  slack_webhook_url = var.slack_webhook_url
  db_identifier     = module.database.identifier
}
