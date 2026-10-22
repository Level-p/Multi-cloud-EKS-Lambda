# ---------------------------------------------------------------------------
# AgriPulse on AWS (eu-west-2): composes the modules in ../modules
#
#   network      VPC: public (ALB, NAT), private (EKS, Lambda), database (RDS) subnets in two zones
#   eks          the cluster for the web app, the incident blog, Redis and the observability stack
#   database     ONE RDS PostgreSQL db.t3.micro, Multi-AZ, IAM authentication, used by all three apps
#   registry     ECR repositories + the Cosign signing key (KMS)
#   storage      S3 media bucket (static images) + GuardDuty malware scanning
#   identity     Cognito: farmers (mobile app), staff (web app and incident blog, through oauth2-proxy)
#   secrets      app secrets in Secrets Manager
#   mobile-api   API Gateway + Lambda (the mobile backend)
#   edge         ONE WAF web ACL on three CloudFront distributions (images, web apps, mobile API) + ALB + DNS
#   monitoring   AWS events Prometheus cannot see (GuardDuty malware, RDS failover) -> EventBridge -> Slack.
#                Every other alert is a Prometheus rule in EKS (OpenTelemetry + CloudWatch via YACE) -> Slack
# In-cluster software (controllers, observability, Argo CD, Kyverno) is the second stack: ../platform.
# ---------------------------------------------------------------------------

locals {
  cluster_name = "${var.name}-eks"
  hosts        = { for k, sub in var.subdomains : k => "${sub}.${var.dns_zone_name}" }

  # PostgreSQL user per app (IAM authentication: the user name is in the rds-db:connect ARN)
  db_users = { web = "agripulse_web", incidents = "agripulse_incidents", mobile = "agripulse_mobile" }
  db_connect_arn = { for k, u in local.db_users :
  k => "arn:aws:rds-db:${var.region}:${data.aws_caller_identity.current.account_id}:dbuser:${module.database.resource_id}/${u}" }
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
  pod_identities = {
    web = {
      namespace = "agripulse", service_account = "agripulse-web", managed_policy_arns = []
      inline_policy = jsonencode({
        Version = "2012-10-17"
        Statement = concat(jsondecode(module.secrets.read_policy_json["web"]).Statement,
        [{ Effect = "Allow", Action = ["rds-db:connect"], Resource = local.db_connect_arn["web"] }])
      })
    }
    incident-blog = {
      namespace = "incident-response", service_account = "incident-blog", managed_policy_arns = []
      inline_policy = jsonencode({
        Version = "2012-10-17"
        Statement = concat(jsondecode(module.secrets.read_policy_json["incidents"]).Statement,
        [{ Effect = "Allow", Action = ["rds-db:connect"], Resource = local.db_connect_arn["incidents"] }])
      })
    }
    # Creates the databases and the IAM-authenticated users with the master password (Argo CD PreSync Job)
    sql-bootstrap = {
      namespace = "agripulse", service_account = "sql-bootstrap", managed_policy_arns = []
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
  }
}

# ---------------- data ----------------
resource "aws_security_group" "mobile_lambda" {
  name        = "${var.name}-mobile-lambda"
  description = "Mobile API Lambda: out to PostgreSQL only"
  vpc_id      = module.network.vpc_id
  tags        = { Name = "${var.name}-mobile-lambda" }
}

resource "aws_vpc_security_group_egress_rule" "mobile_lambda_to_db" {
  security_group_id            = aws_security_group.mobile_lambda.id
  description                  = "PostgreSQL"
  referenced_security_group_id = module.database.security_group_id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

module "database" {
  source              = "../modules/database"
  name                = var.name
  vpc_id              = module.network.vpc_id
  subnet_ids          = module.network.database_subnet_ids
  instance_class      = var.db_instance_class
  deletion_protection = var.deletion_protection
  client_security_group_ids = {
    eks-pods      = module.eks.cluster_security_group_id
    mobile-lambda = aws_security_group.mobile_lambda.id
  }
}

module "registry" {
  source              = "../modules/registry"
  name                = var.name
  deletion_protection = var.deletion_protection
}

module "storage" {
  source              = "../modules/storage"
  name                = var.name
  region              = var.region
  allowed_origins     = ["https://${local.hosts["web"]}"]
  deletion_protection = var.deletion_protection
}

module "identity" {
  source              = "../modules/identity"
  name                = var.name
  staff_hosts         = [local.hosts["web"], local.hosts["incidents"]]
  deletion_protection = var.deletion_protection
}

module "secrets" {
  source               = "../modules/secrets"
  name                 = var.name
  region               = var.region
  oauth2_client_secret = module.identity.oauth2_client_secret
  slack_webhook_url    = var.slack_webhook_url
}

# ---------------- mobile API ----------------
module "mobile_api" {
  source               = "../modules/mobile-api"
  name                 = var.name
  subnet_ids           = module.network.private_subnet_ids
  security_group_id    = aws_security_group.mobile_lambda.id
  user_pool_arn        = module.identity.farmers_user_pool_arn
  db_host              = module.database.endpoint
  db_port              = module.database.port
  db_user              = local.db_users["mobile"]
  db_connect_arn       = local.db_connect_arn["mobile"]
  media_bucket         = module.storage.bucket
  media_bucket_arn     = module.storage.bucket_arn
  media_cdn_domain     = local.hosts["media"]
  origin_verify_secret = random_password.origin_verify.result
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
  subdomains                        = var.subdomains
  vpc_id                            = module.network.vpc_id
  public_subnet_ids                 = module.network.public_subnet_ids
  cluster_security_group_id         = module.eks.cluster_security_group_id
  media_bucket                      = module.storage.bucket
  media_bucket_arn                  = module.storage.bucket_arn
  media_bucket_regional_domain_name = module.storage.bucket_regional_domain_name
  api_invoke_domain                 = module.mobile_api.invoke_domain
  api_stage_name                    = module.mobile_api.stage_name
  origin_verify_secret              = random_password.origin_verify.result
  waf_rate_limit                    = var.waf_rate_limit
  deletion_protection               = var.deletion_protection
}

module "monitoring" {
  source            = "../modules/monitoring"
  name              = var.name
  slack_webhook_url = var.slack_webhook_url
  db_identifier     = module.database.identifier
}
