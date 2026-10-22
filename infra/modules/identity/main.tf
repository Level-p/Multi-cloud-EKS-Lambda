# ---------------------------------------------------------------------------
# Sign-in with Amazon Cognito, two user pools
#   farmers   the mobile app (SRP + refresh tokens, no client secret); API Gateway's Cognito authorizer
#             checks the token on every call except /health
#   staff     agronomists and the incident-response team: oauth2-proxy in front of the web app and the
#             incident blog signs them in with the pool's hosted UI (OIDC authorization code flow).
#             Accounts are created by an administrator only (no self sign-up), MFA required.
# ---------------------------------------------------------------------------

resource "aws_cognito_user_pool" "farmers" {
  name                     = "${var.name}-farmers"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OPTIONAL"
  deletion_protection      = var.deletion_protection ? "ACTIVE" : "INACTIVE"

  software_token_mfa_configuration {
    enabled = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_client" "mobile" {
  name                          = "${var.name}-mobile-app"
  user_pool_id                  = aws_cognito_user_pool.farmers.id
  generate_secret               = false # a mobile app cannot keep a secret: SRP + refresh tokens
  explicit_auth_flows           = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true
  access_token_validity         = 60
  id_token_validity             = 60
  refresh_token_validity        = 30
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

# ---------------- staff: oauth2-proxy (web app, incident blog) ----------------
resource "aws_cognito_user_pool" "staff" {
  name                     = "${var.name}-staff"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "ON"
  deletion_protection      = var.deletion_protection ? "ACTIVE" : "INACTIVE"

  software_token_mfa_configuration {
    enabled = true
  }

  admin_create_user_config {
    allow_admin_create_user_only = true # no self sign-up: an administrator invites each person
  }

  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "random_string" "domain" {
  length  = 6
  special = false
  upper   = false
}

# Hosted UI: https://<prefix>.auth.<region>.amazoncognito.com (the prefix must be unique in the region)
resource "aws_cognito_user_pool_domain" "staff" {
  domain       = "${var.name}-staff-${random_string.domain.result}"
  user_pool_id = aws_cognito_user_pool.staff.id
}

resource "aws_cognito_user_pool_client" "oauth2_proxy" {
  name                                 = "${var.name}-oauth2-proxy"
  user_pool_id                         = aws_cognito_user_pool.staff.id
  generate_secret                      = true # oauth2-proxy is a confidential server-side client
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = [for h in var.staff_hosts : "https://${h}/oauth2/callback"]
  logout_urls                          = [for h in var.staff_hosts : "https://${h}/"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]
  id_token_validity                    = 60
  access_token_validity                = 60
  refresh_token_validity               = 1
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}
