variable "name" {
  type = string
}

variable "staff_hosts" {
  description = "Host names oauth2-proxy serves (its callback and logout URLs)"
  type        = list(string)
}

variable "deletion_protection" {
  type    = bool
  default = false
}
