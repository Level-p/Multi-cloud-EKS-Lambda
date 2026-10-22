variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "cluster_name" {
  description = "EKS cluster that shares the private subnets (subnet tag)"
  type        = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.50.0.0/16"
}

variable "az_count" {
  description = "Availability zones (2 or 3). RDS Multi-AZ and EKS both need at least two."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3."
  }
}
