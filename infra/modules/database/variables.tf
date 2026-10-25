variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  description = "Database subnets in at least two availability zones (Multi-AZ)"
  type        = list(string)
}

variable "client_security_group_ids" {
  description = "Who may connect on 5432: label => security group id (the EKS pods)"
  type        = map(string)
}

variable "instance_class" {
  type    = string
  default = "db.t3.micro"
}

variable "engine_version" {
  description = "PostgreSQL version (newest offered for db.t3.micro Multi-AZ in eu-west-2 at the time of writing)"
  type        = string
  default     = "18.6"
}

variable "deletion_protection" {
  description = "true: the instance cannot be deleted and a final snapshot is taken"
  type        = bool
  default     = false
}
