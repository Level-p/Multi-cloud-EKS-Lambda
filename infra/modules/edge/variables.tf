variable "name" {
  type = string
}

variable "dns_zone_name" {
  description = "Route 53 public hosted zone you own"
  type        = string
}

variable "alb_routes" {
  description = <<-EOT
    Hosts EKS serves through CloudFront "app" and the ALB: key => { host, port (container port of the pods),
    health (path without sign-in), priority (of the ALB rule: fixed, because a changed priority replaces the rule) }.
    One per service and environment (accounts, farm, shop, incidents x staging, production), plus Argo CD and Grafana.
  EOT
  type        = map(object({ host = string, port = number, health = string, priority = number }))
}

variable "api_host" {
  description = "The mobile API (CloudFront \"api\" -> API Gateway)"
  type        = string
}

variable "media_host" {
  description = "Static images (CloudFront \"media\" -> S3)"
  type        = string
}

variable "mobile_gateway_secret" {
  description = "X-Mobile-Gateway value the mobile API Lambda sends: its requests are exempt from the per-IP rate limit"
  type        = string
  sensitive   = true
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "cluster_security_group_id" {
  description = "Security group of the EKS nodes and pods (the ALB reaches the services, Argo CD and Grafana in it)"
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
