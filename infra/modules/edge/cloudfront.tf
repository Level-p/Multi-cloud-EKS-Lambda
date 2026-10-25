# ---------------------------------------------------------------------------
# CloudFront serves ONE thing: the S3 media bucket (listing photos and catalog images), cached at the edge.
#   - Origin Access Control: only this distribution can read the bucket (no public bucket, no website endpoint)
#   - only catalog/ and the uploads GuardDuty tagged NO_THREATS_FOUND are readable (bucket policy below)
#   - HTTPS only (TLS 1.2+), security headers on every response
# The services, Argo CD, Grafana and the mobile API do NOT go through CloudFront: Route 53 points them straight
# at the ALB and at API Gateway, which carry the WAF (waf.tf).
# ---------------------------------------------------------------------------

locals {
  caching_optimized       = "658327ea-f89d-4fab-a63d-7e88639e58f6" # Managed-CachingOptimized
  security_headers_policy = "67f7725c-6f97-4210-82d7-5512b31e9d03" # Managed-SecurityHeadersPolicy
}

resource "aws_cloudfront_origin_access_control" "media" {
  name                              = "${var.name}-media"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "media" {
  #checkov:skip=CKV_AWS_68:Read-only cached images from S3 (GET/HEAD only); the one WAF is regional, on the ALB and API Gateway
  #checkov:skip=CKV2_AWS_47:As above: no WAF on the static image CDN
  enabled         = true
  comment         = "AgriPulse images from S3 (catalog + clean uploads)"
  aliases         = [var.media_host]
  price_class     = "PriceClass_100"
  http_version    = "http2and3"
  is_ipv6_enabled = true

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
