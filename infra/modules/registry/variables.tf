variable "name" {
  type = string
}

variable "repositories" {
  description = "One ECR repository per image: <name>/<repository>"
  type        = list(string)
  default     = ["web", "incident-blog"]
}

variable "deletion_protection" {
  description = "false: a destroy also deletes the repositories that still hold images"
  type        = bool
  default     = false
}
