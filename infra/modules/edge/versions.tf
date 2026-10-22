terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # CloudFront-scope WAF, its log group and CloudFront certificates live in us-east-1
      configuration_aliases = [aws.us_east_1]
    }
  }
}
