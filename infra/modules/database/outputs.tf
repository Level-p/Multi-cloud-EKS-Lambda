output "endpoint" {
  description = "Host name of the primary (it moves to the standby on failover)"
  value       = aws_db_instance.this.address
}

output "port" {
  value = aws_db_instance.this.port
}

output "identifier" {
  value = aws_db_instance.this.identifier
}

output "resource_id" {
  description = "DbiResourceId: the resource in rds-db:connect policies"
  value       = aws_db_instance.this.resource_id
}

output "arn" {
  value = aws_db_instance.this.arn
}

output "master_secret_arn" {
  description = "Secrets Manager secret (JSON username/password) managed by RDS"
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "security_group_id" {
  value = aws_security_group.this.id
}
