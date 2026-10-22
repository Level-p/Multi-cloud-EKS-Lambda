# ---------------------------------------------------------------------------
# EKS: the web app, the incident blog, the Redis cache, Grafana, Prometheus and OpenTelemetry run here.
#   control plane  API endpoint public (GitHub-hosted runners) + private, IAM access entries only
#                  (no aws-auth ConfigMap), secrets encrypted with a KMS key, audit logs, standard support
#   nodes          one managed node group in the private subnets, IMDSv2 with hop limit 1 (pods cannot
#                  borrow the node's role), encrypted gp3 root volumes
#   add-ons        VPC CNI with NetworkPolicy enforcement (the charts' zero-trust policies) and prefix
#                  delegation, CoreDNS, kube-proxy, Pod Identity agent, EBS CSI driver (Tempo/Loki disks)
#   pod identity   AWS permissions per Kubernetes ServiceAccount (pod-identity.tf)
# ---------------------------------------------------------------------------

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# The identity running Terraform (the pipeline's role, or you on a laptop) as an IAM role/user ARN
data "aws_iam_session_context" "current" {
  arn = data.aws_caller_identity.current.arn
}

locals {
  partition = data.aws_partition.current.partition
}

resource "aws_kms_key" "eks" {
  description             = "${var.cluster_name} Kubernetes secrets envelope encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 7
}

resource "aws_kms_alias" "eks" {
  name          = "alias/${var.cluster_name}"
  target_key_id = aws_kms_key.eks.key_id
}

# ---------------- control plane ----------------
resource "aws_iam_role" "cluster" {
  name = "${var.cluster_name}-cluster"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "eks.amazonaws.com" }, Action = ["sts:AssumeRole", "sts:TagSession"] }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_cloudwatch_log_group" "eks" {
  name              = "/aws/eks/${var.cluster_name}/cluster"
  retention_in_days = 30
}

resource "aws_eks_cluster" "this" {
  name     = var.cluster_name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  vpc_config {
    subnet_ids              = var.private_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.public_access_cidrs
  }

  access_config {
    authentication_mode                         = "API" # IAM access entries only
    bootstrap_cluster_creator_admin_permissions = false # admins are the explicit entries below
  }

  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = aws_kms_key.eks.arn
    }
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator"]

  # Standard support only: extended support costs 6x per hour once a version leaves standard support
  upgrade_policy {
    support_type = "STANDARD"
  }

  depends_on = [aws_iam_role_policy_attachment.cluster, aws_cloudwatch_log_group.eks]
}

# ---------------- who may use kubectl / Helm ----------------
locals {
  # The pipeline's role (infra/bootstrap) is always an admin, so an apply from a laptop never locks it out
  pipeline_role_arn = "arn:${local.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${var.pipeline_role_name}"
  cluster_admins    = toset(concat([local.pipeline_role_arn, data.aws_iam_session_context.current.issuer_arn], var.admin_arns))
}

resource "aws_eks_access_entry" "admin" {
  for_each      = local.cluster_admins
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value
}

resource "aws_eks_access_policy_association" "admin" {
  for_each      = local.cluster_admins
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value
  policy_arn    = "arn:${local.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type = "cluster"
  }
  depends_on = [aws_eks_access_entry.admin]
}

# ---------------- nodes ----------------
resource "aws_iam_role" "node" {
  name = "${var.cluster_name}-node"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset(["AmazonEKSWorkerNodePolicy", "AmazonEKS_CNI_Policy", "AmazonEC2ContainerRegistryPullOnly", "AmazonSSMManagedInstanceCore"])

  role       = aws_iam_role.node.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/${each.value}"
}

resource "aws_launch_template" "node" {
  name_prefix = "${var.cluster_name}-node-"

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2
    http_put_response_hop_limit = 1          # containers cannot reach the node's credentials
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 50
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${var.cluster_name}-node" }
  }
}

resource "aws_eks_node_group" "default" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "default"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.private_subnet_ids
  instance_types  = var.node_instance_types
  capacity_type   = "ON_DEMAND"
  ami_type        = "AL2023_x86_64_STANDARD"

  scaling_config {
    min_size     = var.node_min
    desired_size = var.node_desired
    max_size     = var.node_max
  }

  update_config {
    max_unavailable = 1
  }

  launch_template {
    id      = aws_launch_template.node.id
    version = aws_launch_template.node.latest_version
  }

  lifecycle {
    ignore_changes = [scaling_config[0].desired_size] # an autoscaler or you may change it
  }

  depends_on = [aws_iam_role_policy_attachment.node, aws_eks_addon.vpc_cni]
}

# ---------------- add-ons (versions: the default for the cluster's Kubernetes version) ----------------
resource "aws_eks_addon" "vpc_cni" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "vpc-cni"
  resolve_conflicts_on_update = "OVERWRITE"
  # Created before the node group (which depends on it): prefix delegation is on before the first node joins.
  # A DaemonSet with no nodes yet is ACTIVE, so this does not wait for compute.
  configuration_values = jsonencode({
    enableNetworkPolicy = "true" # enforces the charts' NetworkPolicies (default deny, who-may-talk-to-whom)
    env = {
      ENABLE_PREFIX_DELEGATION = "true" # /28 prefixes per ENI: up to 110 pods per node instead of ~35
      WARM_PREFIX_TARGET       = "1"
    }
  })
}

resource "aws_eks_addon" "this" {
  for_each                    = toset(["coredns", "kube-proxy", "eks-pod-identity-agent"])
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.value
  resolve_conflicts_on_update = "OVERWRITE"
  depends_on                  = [aws_eks_node_group.default] # CoreDNS needs a node to become active
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "aws-ebs-csi-driver"
  resolve_conflicts_on_update = "OVERWRITE"

  pod_identity_association {
    role_arn        = aws_iam_role.ebs_csi.arn
    service_account = "ebs-csi-controller-sa"
  }

  depends_on = [aws_eks_node_group.default, aws_eks_addon.this]
}
