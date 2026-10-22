# ---------------------------------------------------------------------------
# Application secrets in AWS Secrets Manager, generated here and never in Git.
# The pods read them through the Secrets Store CSI driver (AWS provider) with EKS Pod Identity: each app's
# ServiceAccount may read its own prefix and the shared one only (read_policies below).
#
#   <name>/web/*         CSRF and cookie secrets of the web app
#   <name>/incidents/*   CSRF and cookie secrets, alert webhook token of the incident blog
#   <name>/shared/*      oauth2-proxy's Cognito app client secret (staff sign-in, modules/identity)
#   <name>/platform/*    Grafana admin password (read by infra/platform)
# The PostgreSQL master password is NOT here: RDS manages it in its own secret (modules/database).
# recovery_window_in_days = 0: a destroy deletes them at once, so a redeploy can reuse the names.
# ---------------------------------------------------------------------------

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  generated = {
    "web/csrf-secret"                 = 32
    "web/cookie-secret"               = 32 # oauth2-proxy cookie secret: 16, 24 or 32 bytes
    "incidents/csrf-secret"           = 32
    "incidents/cookie-secret"         = 32
    "incidents/webhook-token"         = 40
    "platform/grafana-admin-password" = 32
  }
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

resource "aws_secretsmanager_secret" "oauth2_client_secret" {
  name                    = "${var.name}/shared/oauth2-client-secret"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "oauth2_client_secret" {
  secret_id     = aws_secretsmanager_secret.oauth2_client_secret.id
  secret_string = var.oauth2_client_secret
}

data "aws_iam_policy_document" "read" {
  for_each = toset(["web", "incidents"])
  statement {
    actions = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [
      "arn:${data.aws_partition.current.partition}:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:${var.name}/${each.key}/*",
      "arn:${data.aws_partition.current.partition}:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:${var.name}/shared/*",
    ]
  }
}
