variable "name" {
  type = string
}

variable "slack_webhook_url" {
  description = "Slack incoming webhook (the one alert channel)"
  type        = string
  sensitive   = true
}

variable "db_identifier" {
  description = "RDS instance whose events go to Slack"
  type        = string
}
