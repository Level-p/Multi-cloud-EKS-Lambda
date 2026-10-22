variable "name" {
  description = "Secret names start with <name>/"
  type        = string
}

variable "region" {
  type = string
}

variable "oauth2_client_secret" {
  description = "Client secret of the staff sign-in app client (modules/identity)"
  type        = string
  sensitive   = true
}
