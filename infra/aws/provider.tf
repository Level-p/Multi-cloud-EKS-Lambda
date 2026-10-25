provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = var.name, ManagedBy = "terraform" }
  }
}

# CloudFront's certificate (the media distribution) must be created in us-east-1
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
  default_tags {
    tags = { Project = var.name, ManagedBy = "terraform" }
  }
}

terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
    archive = { source = "hashicorp/archive", version = "~> 2.7" }
    tls     = { source = "hashicorp/tls", version = "~> 4.1" } # the sign-in token keys (modules/secrets)
  }
  # First of two stacks: aws -> platform (which reads this state's outputs)
  backend "s3" {
    bucket       = "mfon21-agripulse-tfstate"
    key          = "eks/aws.tfstate"
    region       = "eu-west-2"
    use_lockfile = true
    encrypt      = true
  }
}
