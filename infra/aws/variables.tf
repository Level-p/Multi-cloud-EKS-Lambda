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
  default = { web = "agripulse", incidents = "incidents", api = "api", media = "media", argocd = "argocd", grafana = "grafana" }
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
  description = "Extra IAM users/roles with cluster admin, e.g. [\"arn:aws:iam::<account>:user/<you>\"] for kubectl (GitHub variable CLUSTER_ADMIN_ARNS)"
  type        = list(string)
  default     = []

  # An SSO session (arn:aws:sts::...:assumed-role/<role>/<session>) changes every sign-in: give the role instead
  validation {
    condition     = alltrue([for a in var.cluster_admin_arns : can(regex("^arn:aws:iam::[0-9]{12}:(user|role)/.+$", a))])
    error_message = "Each cluster_admin_arns entry must be an IAM user or role ARN (arn:aws:iam::<account>:user/<name> or :role/<name>). For SSO use the role ARN, e.g. arn:aws:iam::<account>:role/AWSReservedSSO_<...>, not an sts assumed-role ARN."
  }
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

variable "slack_webhook_url" {
  description = "Slack incoming webhook: the ONE channel for every alert (CloudWatch, GuardDuty, RDS events, Alertmanager). The pipeline passes it from the GitHub secret SLACK_WEBHOOK_URL; never write it here."
  type        = string
  sensitive   = true

  validation {
    condition     = startswith(var.slack_webhook_url, "https://hooks.slack.com/")
    error_message = "Set the GitHub secret SLACK_WEBHOOK_URL to a Slack incoming webhook (https://hooks.slack.com/services/...)."
  }
}

variable "deletion_protection" {
  description = "Protect the database (final snapshot), the ALB, the Cognito pools, the ECR images and the media files from deletion"
  type        = bool
  default     = false
}
