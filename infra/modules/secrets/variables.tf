variable "name" {
  description = "Secret names start with <name>/"
  type        = string
}

variable "region" {
  type = string
}

variable "services" {
  description = "The microservices (each gets a CSRF secret per environment)"
  type        = list(string)
}

variable "environments" {
  description = "staging, production: each has its own keys and tokens"
  type        = list(string)
}

variable "slack_webhook_url" {
  description = "Slack incoming webhook for every alert and incident report"
  type        = string
  sensitive   = true
}
