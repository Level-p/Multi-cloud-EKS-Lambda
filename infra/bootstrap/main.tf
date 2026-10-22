# ---------------------------------------------------------------------------
# Lets GitHub Actions sign in to AWS with short-lived OIDC tokens, so no AWS access key is stored anywhere:
# the account's GitHub OIDC provider (created here only if the account has none) + one IAM role that only
# workflows of THIS repository can assume. Applied once from a laptop by ../bootstrap.sh.
# ---------------------------------------------------------------------------

provider "aws" {
  region = "eu-west-2"
  default_tags {
    tags = { Project = "agripulse", ManagedBy = "terraform" }
  }
}

terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {
    bucket       = "mfon21-agripulse-tfstate"
    key          = "eks/bootstrap.tfstate"
    region       = "eu-west-2"
    use_lockfile = true
    encrypt      = true
  }
}

variable "github_repository" {
  description = "GitHub repository allowed to deploy, in OWNER/REPO form (no https://github.com/, no .git)"
  type        = string
  default     = "Level-p/Multi-cloud-EKS-Lambda" # exact case; update it if the repository is renamed

  validation {
    condition     = can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", var.github_repository)) && !endswith(var.github_repository, ".git")
    error_message = "Use OWNER/REPO exactly as in the GitHub URL, without https://github.com/ and without .git (e.g. Level-p/my-repo)."
  }
}

variable "github_subject_prefix" {
  description = "OIDC subject prefix when the repository uses immutable subjects (repo:OWNER@id/REPO@id); bootstrap.sh fills it in"
  type        = string
  default     = ""
}

variable "create_github_oidc_provider" {
  description = "false if the AWS account already has the token.actions.githubusercontent.com provider (bootstrap.sh detects this)"
  type        = bool
  default     = true
}

locals {
  subject_prefix = var.github_subject_prefix != "" ? var.github_subject_prefix : "repo:${var.github_repository}"
}

data "aws_caller_identity" "current" {}

# One GitHub provider per AWS account, shared by every project in it: never created twice
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1", "1c58a3a8518e8759bf075b76b750d4f2df264fcd"]
}

locals {
  github_oidc_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

resource "aws_iam_role" "github_actions" {
  name                 = "agripulse-eks-github-actions" # infra/modules/eks makes this role a cluster admin
  max_session_duration = 7200
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.github_oidc_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        # Only workflows from THIS repository
        StringLike = { "token.actions.githubusercontent.com:sub" = "${local.subject_prefix}:*" }
      }
    }]
  })
}

# Terraform creates the VPC, EKS, RDS, IAM roles, ALB, WAF, CloudFront, Lambda, ECR, KMS...: broad rights.
# For production replace with a policy scoped to those services and the agripulse-* names.
resource "aws_iam_role_policy_attachment" "github_actions_admin" {
  #checkov:skip=CKV_AWS_274:Accepted for this project (trust limited to one repository); scope down for production
  role       = aws_iam_role.github_actions.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

output "aws_role_arn" {
  value = aws_iam_role.github_actions.arn
}
