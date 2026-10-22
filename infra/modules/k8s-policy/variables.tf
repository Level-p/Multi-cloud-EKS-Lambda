variable "app_namespaces" {
  type = list(string)
}

variable "ecr_registry" {
  type = string
}

variable "cosign_public_key_pem" {
  type = string
}

variable "enforce_image_signatures" {
  description = "true: Kyverno REJECTS pods whose agripulse image has no valid Cosign signature. false: it only reports them."
  type        = bool
  default     = true
}
