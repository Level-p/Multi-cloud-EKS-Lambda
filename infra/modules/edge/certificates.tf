# ---------------------------------------------------------------------------
# DNS names on the Route 53 zone, with ACM certificates that Route 53 validates and ACM renews:
#
#   https://<service>.<zone>          -> CloudFront "app" -> ALB -> EKS, production (accounts, farm, shop, incidents)
#   https://<service>.staging.<zone>  -> CloudFront "app" -> ALB -> EKS, staging
#   https://argocd.<zone>, grafana.<zone> -> CloudFront "app" -> ALB -> EKS (their own sign-in)
#   https://api.<zone>        -> CloudFront "api"   -> API Gateway -> Lambda
#   https://media.<zone>      -> CloudFront "media" -> S3 (static images: catalog + clean uploads)
#
# CloudFront's certificate must be in us-east-1 (every name); the ALB's is regional (the ALB names,
# which CloudFront checks because it forwards the viewer's Host header to the ALB).
# ---------------------------------------------------------------------------

data "aws_route53_zone" "this" {
  name         = var.dns_zone_name
  private_zone = false
}

locals {
  alb_hosts = sort([for r in values(var.alb_routes) : r.host])
  hosts     = merge({ for k, r in var.alb_routes : k => r.host }, { api = var.api_host, media = var.media_host })
}

resource "aws_acm_certificate" "cloudfront" {
  provider                  = aws.us_east_1
  domain_name               = var.api_host
  subject_alternative_names = concat([var.media_host], local.alb_hosts)
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate" "alb" {
  domain_name               = local.alb_hosts[0]
  subject_alternative_names = slice(local.alb_hosts, 1, length(local.alb_hosts))
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

# One CNAME per name proves ownership to ACM; the same record serves both certificates of a name
resource "aws_route53_record" "validation" {
  for_each = {
    for o in aws_acm_certificate.cloudfront.domain_validation_options : o.domain_name => o
  }
  zone_id         = data.aws_route53_zone.this.zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "cloudfront" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.cloudfront.arn
  validation_record_fqdns = [for r in aws_route53_record.validation : r.fqdn]
}

resource "aws_acm_certificate_validation" "alb" {
  certificate_arn         = aws_acm_certificate.alb.arn
  validation_record_fqdns = [for o in aws_acm_certificate.alb.domain_validation_options : aws_route53_record.validation[o.domain_name].fqdn]
}
