variable "name" {
  type = string
}

variable "dns_zone_name" {
  description = "Route 53 public hosted zone you own"
  type        = string
}

variable "subdomains" {
  description = "Host name per endpoint under the zone"
  type        = map(string)

  validation {
    condition     = alltrue([for k in ["web", "incidents", "media", "api", "argocd", "grafana"] : contains(keys(var.subdomains), k)])
    error_message = "subdomains needs a value for web, incidents, media, api, argocd and grafana."
  }
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "cluster_security_group_id" {
  description = "Security group of the EKS nodes and pods (the ALB reaches oauth2-proxy, Argo CD and Grafana pods in it)"
  type        = string
}

variable "media_bucket" {
  type = string
}

variable "media_bucket_arn" {
  type = string
}

variable "media_bucket_regional_domain_name" {
  type = string
}

variable "api_invoke_domain" {
  description = "<api id>.execute-api.<region>.amazonaws.com"
  type        = string
}

variable "api_stage_name" {
  type = string
}

variable "origin_verify_secret" {
  description = "Secret CloudFront sends as X-Origin-Verify to the ALB and to API Gateway"
  type        = string
  sensitive   = true
}

variable "waf_rate_limit" {
  description = "Requests per 5 minutes per client IP before AWS WAF blocks"
  type        = number
  default     = 1000
}

variable "deletion_protection" {
  type    = bool
  default = false
}
