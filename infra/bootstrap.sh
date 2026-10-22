#!/bin/bash
# ---------------------------------------------------------------------------
# Run ONCE from your laptop, logged in to AWS (aws configure / aws sso login):
#   1. S3 bucket for ALL Terraform state (versioned, encrypted, private): bootstrap, aws, platform
#   2. GitHub Actions access with OIDC (bootstrap/): one IAM role, no access keys
#   3. Prints the value to store as the GitHub repository secret AWS_ROLE_ARN
# Running it again is safe.
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

# The GitHub repository allowed to deploy (OWNER/REPO, exact case), as in https://github.com/OWNER/REPO
GITHUB_REPO="${GITHUB_REPO:-Level-p/Multi-cloud-EKS-AzureFunctions}"
GITHUB_REPO="${GITHUB_REPO#https://github.com/}"; GITHUB_REPO="${GITHUB_REPO%.git}"; GITHUB_REPO="${GITHUB_REPO%/}"
AWS_REGION="eu-west-2"
AWS_STATE_BUCKET="mfon21-agripulse-tfstate"

echo "== State bucket (all stacks)"
if ! aws s3api head-bucket --bucket "$AWS_STATE_BUCKET" 2>/dev/null; then
  aws s3api create-bucket --bucket "$AWS_STATE_BUCKET" --region "$AWS_REGION" --create-bucket-configuration LocationConstraint="$AWS_REGION"
fi
aws s3api put-bucket-versioning --bucket "$AWS_STATE_BUCKET" --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$AWS_STATE_BUCKET" --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket "$AWS_STATE_BUCKET" --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

echo "== GitHub Actions access to AWS (OIDC) for $GITHUB_REPO"
cd bootstrap
terraform init -input=false
EXTRA_VARS=(-var "github_repository=$GITHUB_REPO")

# One GitHub OIDC provider per AWS account: reuse it if another project created it
if aws iam list-open-id-connect-providers --output text | grep -q "token.actions.githubusercontent.com" \
   && ! terraform state list 2>/dev/null | grep -q "aws_iam_openid_connect_provider.github"; then
  echo "GitHub OIDC provider already exists in this AWS account: reusing it."
  EXTRA_VARS+=(-var "create_github_oidc_provider=false")
fi

# Newer repositories identify themselves as repo:OWNER@id/REPO@id ("immutable subjects"). Ask GitHub.
PREFIX=$(curl -s "https://api.github.com/repos/$GITHUB_REPO/actions/oidc/customization/sub" \
  | grep -o '"sub_claim_prefix": *"[^"]*"' | cut -d'"' -f4 || true)
if [ -n "$PREFIX" ] && [ "$PREFIX" != "repo:$GITHUB_REPO" ]; then
  echo "Repository $GITHUB_REPO uses the immutable subject $PREFIX"
  EXTRA_VARS+=(-var "github_subject_prefix=$PREFIX")
fi

terraform apply -auto-approve -input=false "${EXTRA_VARS[@]}"

echo ""
echo "Done. Add this GitHub repository secret (Settings > Secrets and variables > Actions > Secrets):"
echo "  AWS_ROLE_ARN = $(terraform output -raw aws_role_arn)"
echo "Optional secrets: ANTHROPIC_API_KEY (AI failure diagnosis), SLACK_WEBHOOK_URL, ALARM_EMAIL, GIT_REPO_TOKEN (private repo)"
