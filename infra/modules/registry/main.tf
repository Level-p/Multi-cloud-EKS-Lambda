# ---------------------------------------------------------------------------
# Supply chain on AWS
#   ECR: one repository per EKS app; immutable tags (a deployed tag can never be repointed), scan on
#        push, old images expire. (The mobile API is a Lambda zip, not an image.)
#   KMS: the Cosign signing key (ECC P-256, SIGN_VERIFY). The private key never leaves KMS: the pipeline
#        signs image digests through the KMS API (cosign --key awskms:///alias/...).
#        Kyverno (infra/platform) checks the signatures in the cluster with the public key.
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "app" {
  for_each             = toset(var.repositories)
  name                 = "${var.name}/${each.value}"
  image_tag_mutability = "IMMUTABLE_WITH_EXCLUSION"
  force_delete         = !var.deletion_protection

  # Cosign stores signatures and attestations as tags named after the digest (sha256-<hex>.sig/.att):
  # they are updated in place, every other tag is immutable
  image_tag_mutability_exclusion_filter {
    filter      = "sha256-*"
    filter_type = "WILDCARD"
  }

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  for_each   = aws_ecr_repository.app
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 30 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 30 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_kms_key" "cosign" {
  description              = "${var.name} Cosign image signing key"
  customer_master_key_spec = "ECC_NIST_P256"
  key_usage                = "SIGN_VERIFY"
  deletion_window_in_days  = 7
}

resource "aws_kms_alias" "cosign" {
  name          = "alias/${var.name}-cosign"
  target_key_id = aws_kms_key.cosign.key_id
}

data "aws_kms_public_key" "cosign" {
  key_id = aws_kms_key.cosign.arn
}
