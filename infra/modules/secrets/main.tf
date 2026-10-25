# ---------------------------------------------------------------------------
# Application secrets in AWS Secrets Manager, generated here and never in Git, one set per environment.
# The pods read them through the Secrets Store CSI driver (AWS provider) with EKS Pod Identity: each service's
# ServiceAccount may read its own secrets and its environment's shared ones only (read_policy_json below).
#
#   <name>/<env>/accounts/jwt-private-key   signs the sign-in tokens (RS256): the accounts service only
#   <name>/<env>/shared/jwt-public-key      verifies them: every service
#   <name>/<env>/<service>/csrf-secret      form tokens of each service
#   <name>/<env>/shared/internal-token      the shop's calls to the farm service's /internal/ API
#   <name>/<env>/incidents/webhook-token    Alertmanager's calls to the incident blog
#   <name>/platform/*                       the Slack webhook (every alert and incident report), the mobile gateway
#                                           header (the Lambda and the WAF rate-limit rule)
# The Argo CD and Grafana admin passwords are NOT here: they live only in the cluster (kubectl get secret).
# Staging and production have different keys: a token or cookie of one is useless in the other.
# The PostgreSQL master password is NOT here: RDS manages it in its own secret (modules/database).
# recovery_window_in_days = 0: a destroy deletes them at once, so a redeploy can reuse the names.
# ---------------------------------------------------------------------------

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  generated = merge(
    { for p in setproduct(var.environments, var.services) : "${p[0]}/${p[1]}/csrf-secret" => 48 },
    { for e in var.environments : "${e}/shared/internal-token" => 48 },
    { for e in var.environments : "${e}/incidents/webhook-token" => 40 },
    { "platform/mobile-gateway-secret" = 48 },
  )
  key_pairs = merge(
    { for e in var.environments : "${e}/accounts/jwt-private-key" => tls_private_key.jwt[e].private_key_pem },
    { for e in var.environments : "${e}/shared/jwt-public-key" => tls_private_key.jwt[e].public_key_pem },
  )
  secret_arn = "arn:${data.aws_partition.current.partition}:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:${var.name}"
}

resource "random_password" "this" {
  for_each = local.generated
  length   = each.value
  special  = false
}

resource "aws_secretsmanager_secret" "generated" {
  for_each                = local.generated
  name                    = "${var.name}/${each.key}"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "generated" {
  for_each      = local.generated
  secret_id     = aws_secretsmanager_secret.generated[each.key].id
  secret_string = random_password.this[each.key].result
}

# The token-signing key of each environment (accounts signs, everybody verifies)
resource "tls_private_key" "jwt" {
  for_each  = toset(var.environments)
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "aws_secretsmanager_secret" "keys" {
  for_each                = toset(keys(local.key_pairs))
  name                    = "${var.name}/${each.key}"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "keys" {
  for_each      = toset(keys(local.key_pairs))
  secret_id     = aws_secretsmanager_secret.keys[each.key].id
  secret_string = local.key_pairs[each.key]
}

# The one alert channel: Slack (value from the GitHub secret SLACK_WEBHOOK_URL)
resource "aws_secretsmanager_secret" "slack_webhook" {
  name                    = "${var.name}/platform/slack-webhook-url"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "slack_webhook" {
  secret_id     = aws_secretsmanager_secret.slack_webhook.id
  secret_string = var.slack_webhook_url
}

# What each service in each environment may read: its own secrets and its environment's shared ones
# (the incident blog also reads the Slack webhook: it reports every incident there)
data "aws_iam_policy_document" "read" {
  for_each = { for p in setproduct(var.services, var.environments) : "${p[0]}-${p[1]}" => { service = p[0], env = p[1] } }
  statement {
    actions = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = concat(
      ["${local.secret_arn}/${each.value.env}/${each.value.service}/*", "${local.secret_arn}/${each.value.env}/shared/*"],
      each.value.service == "incidents" ? ["${local.secret_arn}/platform/slack-webhook-url-*"] : [],
    )
  }
}
