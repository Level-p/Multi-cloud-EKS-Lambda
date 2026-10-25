variable "name" {
  type = string
}

variable "service_urls" {
  description = "Public URLs of the production services the gateway calls: accounts, farm, shop"
  type        = map(string)
}

variable "origin_verify_secret" {
  description = "Value CloudFront sends as X-Origin-Verify"
  type        = string
  sensitive   = true
}

variable "mobile_gateway_secret" {
  description = "Value the Lambda sends as X-Mobile-Gateway (exempt from the WAF's per-IP rate limit)"
  type        = string
  sensitive   = true
}

variable "reserved_concurrency" {
  description = <<-EOT
    Maximum concurrent Lambda instances. -1 = no reservation: new accounts have a concurrency quota of 10, all of
    which must stay unreserved. After a quota increase, set e.g. 50.
  EOT
  type        = number
  default     = -1
}
