# ---------------------------------------------------------------------------
# Three CloudFront distributions, ONE WAF web ACL (waf.tf), TLS 1.2+ and HTTPS only for viewers
#
#   media   static images from S3 (Origin Access Control, cached, security headers); only catalog/ and the
#           uploads GuardDuty tagged NO_THREATS_FOUND are readable (bucket policy below)
#   app     agripulse.<zone> + incidents.<zone> -> the ALB (HTTPS), never cached; the viewer's Host header,
#           cookies and query strings go to the origin (oauth2-proxy and the ALB rules need them)
#   api     api.<zone> -> API Gateway's prod stage, never cached; Authorization is forwarded through the cache
#           key (the only way CloudFront forwards it), so a response can never reach another farmer
# The app and api origins receive X-Origin-Verify; the ALB and the Lambda refuse requests without it.
# ---------------------------------------------------------------------------

locals {
  caching_disabled        = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # Managed-CachingDisabled
  caching_optimized       = "658327ea-f89d-4fab-a63d-7e88639e58f6" # Managed-CachingOptimized
  all_viewer              = "216adef6-5c7f-47e4-b989-5492eafa07d3" # Managed-AllViewer (incl. Host)
  all_viewer_except_host  = "b689b0a8-53d0-40ab-baf2-68738e2966ac" # Managed-AllViewerExceptHostHeader
  security_headers_policy = "67f7725c-6f97-4210-82d7-5512b31e9d03" # Managed-SecurityHeadersPolicy
}

# ---------------- media: static images ----------------
resource "aws_cloudfront_origin_access_control" "media" {
  name                              = "${var.name}-media"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "media" {
  enabled         = true
  comment         = "AgriPulse static images (catalog + clean uploads)"
  aliases         = [local.hosts["media"]]
  price_class     = "PriceClass_100"
  http_version    = "http2and3"
  is_ipv6_enabled = true
  web_acl_id      = aws_wafv2_web_acl.this.arn

  origin {
    domain_name              = var.media_bucket_regional_domain_name
    origin_id                = "media"
    origin_access_control_id = aws_cloudfront_origin_access_control.media.id
  }

  default_cache_behavior {
    target_origin_id           = "media"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = local.caching_optimized
    response_headers_policy_id = local.security_headers_policy
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.cloudfront.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

resource "aws_s3_bucket_policy" "media" {
  bucket = var.media_bucket
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [var.media_bucket_arn, "${var.media_bucket_arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "CloudFrontReadsCatalog"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${var.media_bucket_arn}/catalog/*"
        Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.media.arn } }
      },
      {
        # Malware gate: an upload is served only after GuardDuty tagged it clean
        Sid       = "CloudFrontReadsCleanUploads"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${var.media_bucket_arn}/uploads/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn"                                   = aws_cloudfront_distribution.media.arn
            "s3:ExistingObjectTag/GuardDutyMalwareScanStatus" = "NO_THREATS_FOUND"
          }
        }
      },
    ]
  })
}

# ---------------- app: web app + incident blog (ALB) ----------------
resource "aws_cloudfront_distribution" "app" {
  enabled         = true
  comment         = "AgriPulse web app and incident blog (ALB -> EKS)"
  aliases         = [local.hosts["web"], local.hosts["incidents"]]
  price_class     = "PriceClass_100"
  http_version    = "http2and3"
  is_ipv6_enabled = true
  web_acl_id      = aws_wafv2_web_acl.this.arn

  origin {
    domain_name = aws_lb.web.dns_name
    origin_id   = "alb"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only" # the ALB certificate names the viewer's host, which is forwarded
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 60
    }

    custom_header {
      name  = "X-Origin-Verify"
      value = var.origin_verify_secret
    }
  }

  default_cache_behavior {
    target_origin_id         = "alb"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = local.caching_disabled
    origin_request_policy_id = local.all_viewer # Host (ALB rules, TLS), cookies (oauth2-proxy), query strings
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.cloudfront.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

# ---------------- api: mobile API (API Gateway -> Lambda) ----------------
resource "aws_cloudfront_cache_policy" "api" {
  name        = "${var.name}-api-no-cache"
  comment     = "Mobile API: never cached; Authorization is forwarded (it can only be via the cache key)"
  min_ttl     = 0
  default_ttl = 0
  max_ttl     = 1

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_gzip   = true
    enable_accept_encoding_brotli = true
    headers_config {
      header_behavior = "whitelist"
      headers {
        items = ["Authorization"]
      }
    }
    cookies_config {
      cookie_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "all"
    }
  }
}

resource "aws_cloudfront_distribution" "api" {
  enabled         = true
  comment         = "AgriPulse mobile API (API Gateway -> Lambda)"
  aliases         = [local.hosts["api"]]
  price_class     = "PriceClass_100"
  http_version    = "http2and3"
  is_ipv6_enabled = true
  web_acl_id      = aws_wafv2_web_acl.this.arn

  origin {
    domain_name = var.api_invoke_domain
    origin_id   = "api-gateway"
    origin_path = "/${var.api_stage_name}"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }

    custom_header {
      name  = "X-Origin-Verify"
      value = var.origin_verify_secret
    }
  }

  default_cache_behavior {
    target_origin_id           = "api-gateway"
    viewer_protocol_policy     = "https-only" # an API client must not be redirected with its token
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = aws_cloudfront_cache_policy.api.id
    origin_request_policy_id   = local.all_viewer_except_host # API Gateway routes on its own host name
    response_headers_policy_id = local.security_headers_policy
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.cloudfront.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

# ---------------- DNS ----------------
locals {
  aliases = {
    web       = aws_cloudfront_distribution.app
    incidents = aws_cloudfront_distribution.app
    api       = aws_cloudfront_distribution.api
    media     = aws_cloudfront_distribution.media
  }
}

resource "aws_route53_record" "alias" {
  for_each = merge([for k in keys(local.aliases) : { for t in ["A", "AAAA"] : "${k}-${t}" => { host = k, type = t } }]...)
  zone_id  = data.aws_route53_zone.this.zone_id
  name     = local.hosts[each.value.host]
  type     = each.value.type
  alias {
    name                   = local.aliases[each.value.host].domain_name
    zone_id                = local.aliases[each.value.host].hosted_zone_id
    evaluate_target_health = false
  }
}
