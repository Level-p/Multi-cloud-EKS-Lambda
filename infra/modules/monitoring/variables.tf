variable "name" {
  type = string
}

variable "alarm_email" {
  description = "E-mail subscribed to the alarm topic (confirm the subscription e-mail). Empty = no subscription"
  type        = string
  default     = ""
}

variable "webhook_url" {
  description = "The incident blog's CloudWatch webhook (https://incidents.<zone>/webhooks/cloudwatch)"
  type        = string
}

variable "webhook_token_secret_arn" {
  type = string
}

variable "alb_arn_suffix" {
  type = string
}

variable "target_group_arn_suffixes" {
  type = map(string)
}

variable "db_identifier" {
  type = string
}

variable "db_connection_alarm_threshold" {
  description = "Alarm above this many PostgreSQL connections (db.t3.micro: max_connections is about 80)"
  type        = number
  default     = 65
}
