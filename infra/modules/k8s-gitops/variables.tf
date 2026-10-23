variable "app_namespaces" {
  description = "Argo CD Application (= chart under gitops/charts) => namespace"
  type        = map(string)
  default     = { agripulse-web = "agripulse", incident-blog = "incident-response" }
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

variable "common_values" {
  description = "Values for both charts: image.registry, aws.*, db.*, auth.* (see gitops/charts/*/values.yaml)"
  type        = any
}

variable "app_values" {
  description = "Per-chart values merged over common_values (each needs an aws and a db map)"
  type        = any
}

variable "argocd_host" {
  description = "Public host name of Argo CD (argocd.<zone>, served through CloudFront + WAF + the ALB)"
  type        = string
}
