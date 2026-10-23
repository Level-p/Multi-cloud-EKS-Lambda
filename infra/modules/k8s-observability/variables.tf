variable "region" {
  description = "Region YACE reads CloudWatch metrics from"
  type        = string
}

variable "project_tag" {
  description = "Value of the Project tag on every AWS resource of this project (YACE finds them by it)"
  type        = string
  default     = "agripulse"
}

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

variable "slack_webhook_secret_arn" {
  description = "Secrets Manager secret with the Slack incoming webhook (created by infra/aws): every alert goes there"
  type        = string
}

variable "grafana_host" {
  description = "Public host name of Grafana (grafana.<zone>, served through CloudFront + WAF + the ALB)"
  type        = string
}
