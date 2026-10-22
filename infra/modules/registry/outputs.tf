output "ecr_registry" {
  value = split("/", values(aws_ecr_repository.app)[0].repository_url)[0]
}

output "cosign_key_uri" {
  description = "cosign sign --key <this> (AWS credentials and AWS_REGION in the environment)"
  value       = "awskms:///${aws_kms_alias.cosign.name}"
}

output "cosign_public_key_pem" {
  description = "Kyverno verifies image signatures with this (infra/platform)"
  value       = data.aws_kms_public_key.cosign.public_key_pem
}
