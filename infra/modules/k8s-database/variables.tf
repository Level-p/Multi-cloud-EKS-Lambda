variable "databases" {
  description = "<service>-<environment> => { name, user } (infra/aws output \"databases\")"
  type        = map(object({ name = string, user = string }))

  validation {
    condition     = alltrue([for d in values(var.databases) : can(regex("^[a-z][a-z0-9_]{0,62}$", d.name)) && can(regex("^[a-z][a-z0-9_]{0,62}$", d.user))])
    error_message = "Database and user names: lowercase letters, digits and _ only (they are written into SQL)."
  }
}

variable "db_host" {
  type = string
}

variable "db_port" {
  type = number
}

variable "master_secret_arn" {
  description = "The RDS-managed master secret (JSON: username, password)"
  type        = string
}

variable "region" {
  type = string
}

variable "image" {
  description = "PostgreSQL client image, pinned by digest (the official image, from the ECR Public mirror of Docker Hub)"
  type        = string
  default     = "public.ecr.aws/docker/library/postgres:18-alpine@sha256:898c60cb390d1085bc97bdd6a1ae968757421df394b071848b52b1c09c89d6a5"
}

variable "rds_ca_sha256" {
  description = "Checksum of the RDS CA bundle (the same as in app/Dockerfile)"
  type        = string
  default     = "90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b"
}
