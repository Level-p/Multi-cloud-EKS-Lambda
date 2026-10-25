variable "deployments" {
  description = "<service>-<environment> (= namespace = Argo CD Application) => { service, environment, values (Helm values only Terraform knows) }"
  type = map(object({
    service     = string
    environment = string
    values      = any
  }))
}

variable "redis_deployments" {
  description = "Deployments whose service runs a Valkey cache (gets a password Secret <service>-redis)"
  type        = list(string)
  default     = []
}

variable "git_repo_url" {
  type = string
}

variable "git_repo_token" {
  type      = string
  default   = ""
  sensitive = true
}

variable "git_revision" {
  type    = string
  default = "main"
}

variable "argocd_host" {
  description = "Public host name of Argo CD (argocd.<zone>, served through the ALB + WAF)"
  type        = string
}
