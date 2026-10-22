variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "allowed_origins" {
  description = "Web origins allowed to upload media with the pre-signed POST (the mobile app needs none)"
  type        = list(string)
}

variable "deletion_protection" {
  description = "false: a destroy also deletes the bucket's files"
  type        = bool
  default     = false
}
