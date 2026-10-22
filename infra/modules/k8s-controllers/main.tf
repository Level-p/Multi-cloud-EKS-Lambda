# ---------------------------------------------------------------------------
# Cluster controllers. Their AWS permissions come from EKS Pod Identity (modules/eks, pod-identity.tf):
# no access keys, no IRSA annotations.
# ---------------------------------------------------------------------------

# The ALB, its listener and target groups belong to Terraform (modules/edge, alb.tf). The controller only
# registers the pod IPs of each app's TargetGroupBinding: it creates no load balancer of its own.
resource "helm_release" "aws_load_balancer_controller" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = "3.6.1"
  namespace  = "kube-system"

  values = [yamlencode({
    clusterName                 = var.cluster_name
    region                      = var.region
    vpcId                       = var.vpc_id
    replicaCount                = 2
    serviceAccount              = { create = true, name = "aws-load-balancer-controller" } # Pod Identity association by this name
    enableServiceMutatorWebhook = false                                                    # never turn ordinary Services into load balancers
    createIngressClassResource  = false                                                    # no Ingress objects: TargetGroupBinding only
    serviceMonitor              = { enabled = true }
  })]

  # The ServiceMonitor CRD comes from kube-prometheus-stack: the root makes this module wait for k8s-observability
}

# Secrets Manager -> files in the pod and a synced Kubernetes Secret (SecretProviderClass in each app chart)
resource "helm_release" "secrets_store_csi" {
  name       = "secrets-store-csi-driver-provider-aws"
  repository = "https://aws.github.io/secrets-store-csi-driver-provider-aws"
  chart      = "secrets-store-csi-driver-provider-aws"
  version    = "3.1.4"
  namespace  = "kube-system"

  values = [yamlencode({
    awsRegion = var.region
    "secrets-store-csi-driver" = {
      install              = true               # the CSI driver itself (subchart)
      syncSecret           = { enabled = true } # env vars from the synced Kubernetes Secret
      enableSecretRotation = true
      rotationPollInterval = "2m"
    }
  })]
}
