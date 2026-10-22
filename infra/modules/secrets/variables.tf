variable "name" {
  description = "Secret names start with <name>/"
  type        = string
}

variable "region" {
  type = string
}

variable "slack_webhook_url" {
  description = "Slack incoming webhook for every alert"
  type        = string
  sensitive   = true
}

variable "oauth2_client_secret" {
  description = "Client secret of the staff sign-in app client (modules/identity)"
  type        = string
  sensitive   = true
}
