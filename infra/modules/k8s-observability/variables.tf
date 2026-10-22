variable "app_namespaces" {
  description = "Namespaces whose pods may send OTLP to the collector"
  type        = list(string)
}

variable "webhook_token_secret_arn" {
  description = "The incident blog's webhook token (Alertmanager sends it as a Bearer header)"
  type        = string
}

variable "grafana_password_secret_arn" {
  type = string
}

variable "slack_webhook_url" {
  type      = string
  default   = ""
  sensitive = true
}
