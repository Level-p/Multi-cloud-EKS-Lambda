output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  value = aws_eks_cluster.this.endpoint
}

output "cluster_ca" {
  value = aws_eks_cluster.this.certificate_authority[0].data
}

output "cluster_security_group_id" {
  description = "The security group of the nodes and pods (managed node group without custom groups)"
  value       = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}

output "pod_role_arns" {
  value = { for k, r in aws_iam_role.pod : k => r.arn }
}
