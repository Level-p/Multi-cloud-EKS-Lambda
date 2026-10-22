# ---------------------------------------------------------------------------
# DNS names on the Route 53 zone, with ACM certificates that Route 53 validates and ACM renews:
#
#   https://agripulse.<zone>  -> CloudFront "app"   -> ALB -> EKS (oauth2-proxy -> agripulse-web)
#   https://incidents.<zone>  -> CloudFront "app"   -> ALB -> EKS (oauth2-proxy -> incident-blog)
#   https://api.<zone>        -> CloudFront "api"   -> API Gateway -> Lambda
#   https://media.<zone>      -> CloudFront "media" -> S3 (static images: catalog + clean uploads)
#
# CloudFront's certificate must be in us-east-1 (all four names); the ALB's is regional (the two app names,
# which CloudFront checks because it forwards the viewer's Host header to the ALB).
# ---------------------------------------------------------------------------

data "aws_route53_zone" "this" {
  name         = var.dns_zone_name
  private_zone = false
}

locals {
  hosts = { for k, sub in var.subdomains : k => "${sub}.${var.dns_zone_name}" }
}

resource "aws_acm_certificate" "cloudfront" {
  provider                  = aws.us_east_1
  domain_name               = local.hosts["web"]
  subject_alternative_names = [for k in ["incidents", "api", "media"] : local.hosts[k]]
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate" "alb" {
  domain_name               = local.hosts["web"]
  subject_alternative_names = [local.hosts["incidents"]]
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
