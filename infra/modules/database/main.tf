# ---------------------------------------------------------------------------
# The ONE database of the project: Amazon RDS for PostgreSQL
#
#   instance      db.t3.micro, Multi-AZ: a synchronous standby in a second availability zone, automatic
#                 failover (60-120 s, same endpoint), backups taken from the standby
#   databases     agripulse (web app, EKS), incidents (incident blog, EKS), mobile (mobile API, Lambda)
#   who connects  each app as its own database user with IAM database authentication: a 15-minute token
#                 signed with the app's IAM role (EKS Pod Identity, Lambda execution role), no password
#   master        password generated and rotated by RDS in Secrets Manager; only the SQL bootstrap Job
#                 (Argo CD PreSync hook) reads it, to create the databases and the IAM-authenticated users
#   network       database subnets with no route out; 5432 only from the security groups the caller admits
#   TLS           rds.force_ssl = 1: every connection is encrypted (the apps verify the RDS CA bundle)
# ---------------------------------------------------------------------------

resource "aws_db_subnet_group" "this" {
  name       = "${var.name}-postgres"
  subnet_ids = var.subnet_ids
}

resource "aws_security_group" "this" {
  name        = "${var.name}-postgres"
  description = "PostgreSQL: 5432 from the app security groups only (rules added by the callers)"
  vpc_id      = var.vpc_id
  tags        = { Name = "${var.name}-postgres" }
}

resource "aws_vpc_security_group_ingress_rule" "clients" {
  for_each                     = var.client_security_group_ids
  security_group_id            = aws_security_group.this.id
  description                  = "PostgreSQL from ${each.key}"
  referenced_security_group_id = each.value
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

# name_prefix: a generated name, so a group left behind by a failed apply never blocks the next one, and a
# change that replaces the group creates the new one before the instance moves off the old one
resource "aws_db_parameter_group" "this" {
  name_prefix = "${var.name}-postgres18-"
  family      = "postgres18"

  lifecycle {
    create_before_destroy = true
  }

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "500" # slow statements (ms) go to the PostgreSQL log in CloudWatch
  }
  # PostgreSQL 18 made log_connections a list of stages instead of on/off; these three are what "on" logged before
  parameter {
    name  = "log_connections"
    value = "receipt,authentication,authorization"
  }
}

resource "aws_db_instance" "this" {
  identifier     = "${var.name}-postgres"
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class
  multi_az       = true

  allocated_storage     = 20
  max_allocated_storage = 100 # storage autoscaling
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name                             = "postgres"
  username                            = "agripulse_admin"
  manage_master_user_password         = true # RDS generates and rotates it in Secrets Manager
  iam_database_authentication_enabled = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.this.id]
  publicly_accessible    = false
  parameter_group_name   = aws_db_parameter_group.this.name
  ca_cert_identifier     = "rds-ca-rsa2048-g1"

  backup_retention_period         = 7
  backup_window                   = "02:00-03:00"
  maintenance_window              = "sun:03:30-sun:04:30"
  auto_minor_version_upgrade      = true
  copy_tags_to_snapshot           = true
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  deletion_protection             = var.deletion_protection
  skip_final_snapshot             = !var.deletion_protection
  final_snapshot_identifier       = var.deletion_protection ? "${var.name}-postgres-final" : null

  lifecycle {
    ignore_changes = [engine_version] # minor versions upgrade themselves in the maintenance window
  }
}
