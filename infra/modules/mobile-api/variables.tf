variable "name" {
  type = string
}

variable "subnet_ids" {
  description = "Private subnets for the Lambda's network interfaces"
  type        = list(string)
}

variable "security_group_id" {
  description = "The Lambda's security group (admitted by the database's)"
  type        = string
}

variable "user_pool_arn" {
  description = "Cognito user pool of the farmers (API Gateway authorizer)"
  type        = string
}

variable "db_host" {
  type = string
}

variable "db_port" {
  type = number
}

variable "db_user" {
  type    = string
  default = "agripulse_mobile"
}

variable "db_connect_arn" {
  description = "arn:aws:rds-db:<region>:<account>:dbuser:<DbiResourceId>/<db_user>"
  type        = string
}

variable "media_bucket" {
  type = string
}

variable "media_bucket_arn" {
  type = string
}

variable "media_cdn_domain" {
  type = string
}

variable "origin_verify_secret" {
  description = "Value CloudFront sends as X-Origin-Verify"
  type        = string
  sensitive   = true
}

variable "reserved_concurrency" {
  description = <<-EOT
    Maximum concurrent Lambda instances = maximum PostgreSQL connections the mobile API can open (db.t3.micro
    allows ~80 in total). -1 = no reservation: new accounts have a concurrency quota of 10, all of which must
    stay unreserved, and that quota then caps the connections by itself. After a quota increase, set e.g. 20.
  EOT
  type        = number
  default     = -1
}
