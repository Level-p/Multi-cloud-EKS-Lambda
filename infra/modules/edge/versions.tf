terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # CloudFront's certificate (media) lives in us-east-1
      configuration_aliases = [aws.us_east_1]
    }
  }
}
