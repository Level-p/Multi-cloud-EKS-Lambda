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
#
# Both certificates name WILDCARDS (*.<zone>, *.staging.<zone>), never one name per host: ACM allows 10 names per
# certificate by default, and a name per service x environment passed that at 4 services (12 names: ACM answers
# LimitExceededException, which the provider retries for an hour). A wildcard covers every host one label below
# it, so a new service or environment adds no certificate name. The preconditions stop the PLAN if new hosts
# ever need more wildcards than the quota allows.
# ---------------------------------------------------------------------------

data "aws_route53_zone" "this" {
  name         = var.dns_zone_name
  private_zone = false
}

locals {
  alb_hosts = sort([for r in values(var.alb_routes) : r.host])
  hosts     = merge({ for k, r in var.alb_routes : k => r.host }, { api = var.api_host, media = var.media_host })

  # The certificate name that covers a host: the wildcard of its parent (shop.staging.<zone> -> *.staging.<zone>);
  # the zone apex is named as itself
  cert_name             = { for h in values(local.hosts) : h => h == var.dns_zone_name ? h : replace(h, "/^[^.]+[.]/", "*.") }
  cloudfront_cert_names = sort(distinct(values(local.cert_name)))
  alb_cert_names        = sort(distinct([for h in local.alb_hosts : local.cert_name[h]]))
  acm_max_names         = 10 # ACM "Domain names per ACM certificate", default quota
}

resource "aws_acm_certificate" "cloudfront" {
  provider                  = aws.us_east_1
  domain_name               = local.cloudfront_cert_names[0]
  subject_alternative_names = slice(local.cloudfront_cert_names, 1, length(local.cloudfront_cert_names))
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
    precondition {
      condition     = length(local.cloudfront_cert_names) <= local.acm_max_names
      error_message = "The CloudFront certificate would name ${length(local.cloudfront_cert_names)} domains (${join(", ", local.cloudfront_cert_names)}); ACM allows ${local.acm_max_names}. Put new hosts one label below the zone or below staging.<zone>, where a wildcard already covers them."
    }
  }
}

resource "aws_acm_certificate" "alb" {
  domain_name               = local.alb_cert_names[0]
  subject_alternative_names = slice(local.alb_cert_names, 1, length(local.alb_cert_names))
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
    precondition {
      condition     = length(local.alb_cert_names) <= local.acm_max_names
      error_message = "The ALB certificate would name ${length(local.alb_cert_names)} domains (${join(", ", local.alb_cert_names)}); ACM allows ${local.acm_max_names}."
    }
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
