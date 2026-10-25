# Second of two stacks: aws -> platform. Everything here lives INSIDE the EKS cluster.
# The aws stack's outputs come from its state in the same S3 bucket, and Helm/Kubernetes sign in to EKS
# with the pipeline's IAM role (aws eks get-token).

data "terraform_remote_state" "aws" {
  backend = "s3"
  config = {
    bucket = "mfon21-agripulse-tfstate"
    key    = "eks/aws.tfstate"
    region = "eu-west-2"
  }
}

locals {
  aws            = data.terraform_remote_state.aws.outputs
  eks_token_args = ["eks", "get-token", "--cluster-name", local.aws.cluster_name, "--region", local.aws.region, "--output", "json"]
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "agripulse", ManagedBy = "terraform" }
  }
}

provider "helm" {
  kubernetes = {
    host                   = local.aws.cluster_endpoint
    cluster_ca_certificate = base64decode(local.aws.cluster_ca)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = local.eks_token_args
    }
  }
}

provider "kubernetes" {
  host                   = local.aws.cluster_endpoint
  cluster_ca_certificate = base64decode(local.aws.cluster_ca)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = local.eks_token_args
  }
}

terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 6.0" }
    helm       = { source = "hashicorp/helm", version = "~> 3.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 3.0" }
    random     = { source = "hashicorp/random", version = "~> 3.6" }
    http       = { source = "hashicorp/http", version = "~> 3.4" } # the RDS CA bundle (modules/k8s-database)
  }

  backend "s3" {
    bucket       = "mfon21-agripulse-tfstate"
    key          = "eks/platform.tfstate"
    region       = "eu-west-2"
    use_lockfile = true
    encrypt      = true
  }
}
