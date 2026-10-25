variable "region" {
  description = "Region of infra/aws (EKS, Secrets Manager)"
  type        = string
  default     = "eu-west-2"
}

variable "git_repo_url" {
  description = "HTTPS URL of THIS repository, e.g. https://github.com/Level-p/Multi-cloud-EKS-Lambda.git (Argo CD reads gitops/ from it). The platform workflow passes its own repository URL automatically."
  type        = string

  validation {
    condition     = startswith(var.git_repo_url, "https://")
    error_message = "Use the HTTPS clone URL of the repository."
  }
}

variable "git_repo_token" {
  description = "Read-only token for the Git repository (GitHub fine-grained PAT: this repository, Contents read-only); empty for a public repo"
  type        = string
  default     = ""
  sensitive   = true
}

variable "git_revision" {
  type    = string
  default = "main"
}

variable "enforce_image_signatures" {
  description = "true: Kyverno REJECTS pods in the app namespaces whose agripulse image has no valid Cosign signature. false: it only reports them (PolicyReports), e.g. while the first signed images are being built."
  type        = bool
  default     = true
}
