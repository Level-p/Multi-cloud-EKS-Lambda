# ---------------------------------------------------------------------------
# DNS names on the Route 53 zone, with ACM certificates that Route 53 validates and ACM renews:
#
#   https://<service>.<zone>               -> ALB (+ WAF) -> EKS, production (accounts, farm, shop, incidents)
#   https://<service>.staging.<zone>       -> ALB (+ WAF) -> EKS, staging
#   https://argocd.<zone>, grafana.<zone>  -> ALB (+ WAF) -> EKS (their own sign-in)
#   https://api.<zone>                     -> API Gateway custom domain (+ WAF) -> Lambda
#   https://media.<zone>                   -> CloudFront -> S3 (catalog images + clean uploads)
#
# Two certificates: a regional one (eu-west-2) for the ALB and API Gateway, and one in us-east-1 for CloudFront,
# which only accepts certificates from there.
#
# Both name WILDCARDS (*.<zone>, *.staging.<zone>), never one name per host: ACM allows 10 names per certificate
# by default, and a name per service x environment passed that at 4 services (12 names: ACM answers
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
  regional_cert_names   = sort(distinct([for h in concat(local.alb_hosts, [var.api_host]) : local.cert_name[h]]))
  cloudfront_cert_names = [local.cert_name[var.media_host]]
  acm_max_names         = 10 # ACM "Domain names per ACM certificate", default quota
}

# The ALB (every service host, Argo CD, Grafana) and the API Gateway custom domain
resource "aws_acm_certificate" "regional" {
  domain_name               = local.regional_cert_names[0]
  subject_alternative_names = slice(local.regional_cert_names, 1, length(local.regional_cert_names))
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
    precondition {
      condition     = length(local.regional_cert_names) <= local.acm_max_names
      error_message = "The regional certificate would name ${length(local.regional_cert_names)} domains (${join(", ", local.regional_cert_names)}); ACM allows ${local.acm_max_names}. Put new hosts one label below the zone or below staging.<zone>, where a wildcard already covers them."
    }
  }
}

# CloudFront (media only)
resource "aws_acm_certificate" "cloudfront" {
  provider          = aws.us_east_1
  domain_name       = local.cloudfront_cert_names[0]
  validation_method = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

# One CNAME per name proves ownership to ACM; the same record serves both certificates of a name
resource "aws_route53_record" "validation" {
  for_each = merge(
    { for o in aws_acm_certificate.cloudfront.domain_validation_options : o.domain_name => o },
    { for o in aws_acm_certificate.regional.domain_validation_options : o.domain_name => o },
  )
  zone_id         = data.aws_route53_zone.this.zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "regional" {
  certificate_arn         = aws_acm_certificate.regional.arn
  validation_record_fqdns = [for o in aws_acm_certificate.regional.domain_validation_options : aws_route53_record.validation[o.domain_name].fqdn]
}

resource "aws_acm_certificate_validation" "cloudfront" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.cloudfront.arn
  validation_record_fqdns = [for o in aws_acm_certificate.cloudfront.domain_validation_options : aws_route53_record.validation[o.domain_name].fqdn]
}

# ---------------- DNS: the services and tools -> ALB; the mobile API -> API Gateway; media -> CloudFront ----------------
resource "aws_route53_record" "alb" {
  for_each = var.alb_routes
  zone_id  = data.aws_route53_zone.this.zone_id
  name     = each.value.host
  type     = "A"
  alias {
    name                   = aws_lb.web.dns_name
    zone_id                = aws_lb.web.zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "api" {
  zone_id = data.aws_route53_zone.this.zone_id
  name    = aws_api_gateway_domain_name.api.domain_name
  type    = "A"
  alias {
    name                   = aws_api_gateway_domain_name.api.regional_domain_name
    zone_id                = aws_api_gateway_domain_name.api.regional_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "media" {
  for_each = toset(["A", "AAAA"])
  zone_id  = data.aws_route53_zone.this.zone_id
  name     = var.media_host
  type     = each.key
  alias {
    name                   = aws_cloudfront_distribution.media.domain_name
    zone_id                = aws_cloudfront_distribution.media.hosted_zone_id
    evaluate_target_health = false
  }
}
