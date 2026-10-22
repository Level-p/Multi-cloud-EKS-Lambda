variable "region" {
  type    = string
  default = "eu-west-2"
}

variable "name" {
  type    = string
  default = "agripulse"
}

variable "dns_zone_name" {
  description = "Route 53 public hosted zone you own; the endpoints get <subdomain>.<zone>"
  type        = string
  default     = "mfon21.space"
}

variable "subdomains" {
  type    = map(string)
  default = { web = "agripulse", incidents = "incidents", api = "api", media = "media" }
}

variable "az_count" {
  description = "Availability zones (2: EKS nodes and the RDS primary/standby spread over two zones)"
  type        = number
  default     = 2
}

# ---------------- EKS ----------------
variable "kubernetes_version" {
  description = "EKS version (null = the default for new clusters)"
  type        = string
  default     = null
}

variable "node_instance_types" {
  type    = list(string)
  default = ["t3.large"]
}

variable "node_desired" {
  type    = number
  default = 3
}

variable "cluster_admin_arns" {
  description = "Extra IAM users/roles with cluster admin, e.g. [\"arn:aws:iam::<account>:user/<you>\"] for kubectl"
  type        = list(string)
  default     = []
}

# ---------------- database ----------------
variable "db_instance_class" {
  type    = string
  default = "db.t3.micro"
}

# ---------------- edge, operations ----------------
variable "waf_rate_limit" {
  description = "Requests per 5 minutes per client IP before AWS WAF blocks"
  type        = number
  default     = 1000
}

variable "alarm_email" {
  description = "E-mail subscribed to the alarm topic (confirm the subscription e-mail). Empty = no subscription"
  type        = string
  default     = ""
}

variable "deletion_protection" {
  description = "Protect the database (final snapshot), the ALB, the Cognito pools, the ECR images and the media files from deletion"
  type        = bool
  default     = false
}
