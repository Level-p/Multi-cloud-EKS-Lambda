# ---------------------------------------------------------------------------
# EKS Pod Identity: AWS permissions per Kubernetes ServiceAccount, no access keys, no IRSA annotations.
#   built in    EBS CSI driver, AWS Load Balancer Controller (its official policy, v3.6.1)
#   var.pod_identities   the apps' own roles, composed by the caller (Secrets Manager, rds-db:connect, ECR read).
#   Every role has an inline policy, so the role set is known at plan time even when a policy is not.
# A ServiceAccount may be created later (by Helm or Argo CD): the association waits for it.
# ---------------------------------------------------------------------------

locals {
  pod_trust = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "pods.eks.amazonaws.com" }, Action = ["sts:AssumeRole", "sts:TagSession"] }]
  })
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${var.cluster_name}-ebs-csi"
  assume_role_policy = local.pod_trust
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

locals {
  pod_identities = merge({
    load-balancer-controller = {
      namespace           = "kube-system"
      service_account     = "aws-load-balancer-controller"
      managed_policy_arns = []
      inline_policy       = file("${path.module}/policies/aws-load-balancer-controller.json")
    }
  }, var.pod_identities)
}

resource "aws_iam_role" "pod" {
  for_each           = local.pod_identities
  name               = "${var.cluster_name}-${each.key}"
  assume_role_policy = local.pod_trust
}

resource "aws_iam_role_policy_attachment" "pod" {
  for_each = merge([for k, r in local.pod_identities : { for p in r.managed_policy_arns : "${k}/${basename(p)}" => { role = k, policy = p } }]...)

  role       = aws_iam_role.pod[each.value.role].name
  policy_arn = each.value.policy
}

resource "aws_iam_role_policy" "pod" {
  for_each = local.pod_identities
  name     = each.key
  role     = aws_iam_role.pod[each.key].id
  policy   = each.value.inline_policy
}

resource "aws_eks_pod_identity_association" "this" {
  for_each        = local.pod_identities
  cluster_name    = aws_eks_cluster.this.name
  namespace       = each.value.namespace
  service_account = each.value.service_account
  role_arn        = aws_iam_role.pod[each.key].arn
}
