# ---------------------------------------------------------------------------
# ONE AWS WAF web ACL (regional, eu-west-2) in front of everything that is not a static image: the same rules
# protect every service in both environments, Argo CD and Grafana (the ALB) and the mobile API (the API Gateway
# stage). The images on CloudFront are read-only cached S3 objects.
#
#   AWSManagedRulesCommonRuleSet          OWASP Top 10 basics: XSS, LFI/RFI, bad paths, oversize headers
#   AWSManagedRulesSQLiRuleSet            SQL injection in the query string, body, cookies and URI
#   AWSManagedRulesKnownBadInputsRuleSet  exploit patterns (Log4j, Java deserialisation, ...)
#   AWSManagedRulesAmazonIpReputationList known bad IPs (bots, scanners, botnets)
#   AWSManagedRulesBotControlRuleSet      scrapers and bad bots; HTTP libraries only COUNT (the mobile app is one)
#   rate-limit-per-ip                     requests per 5 minutes per client IP (not the mobile API gateway's)
#   block-scripted-sign-in                curl/wget/python on the sign-in and registration endpoints (credential
#                                         stuffing, fake sign-ups)
# The apps still validate everything and only use bound SQL parameters (defence in depth).
# Blocked and counted requests are logged to CloudWatch.
# ---------------------------------------------------------------------------

locals {
  managed_rules = ["AWSManagedRulesCommonRuleSet", "AWSManagedRulesSQLiRuleSet", "AWSManagedRulesKnownBadInputsRuleSet",
  "AWSManagedRulesAmazonIpReputationList"]
}

resource "aws_wafv2_regex_pattern_set" "scripted_clients" {
  name  = "${var.name}-scripted-clients"
  scope = "REGIONAL"
  regular_expression {
    regex_string = "(curl|wget|python-requests|go-http-client|scrapy|httpclient)"
  }
}

# The accounts service's sign-in, registration and token endpoints (web forms and the mobile app's API)
resource "aws_wafv2_regex_pattern_set" "sign_in_paths" {
  name  = "${var.name}-sign-in-paths"
  scope = "REGIONAL"
  regular_expression {
    regex_string = "^/(login|register|api/token|api/register)/?$"
  }
}

resource "aws_wafv2_web_acl" "this" {
  name  = "${var.name}-edge"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  dynamic "rule" {
    for_each = local.managed_rules
    content {
      name     = rule.value
      priority = rule.key
      override_action {
        none {}
      }
      statement {
        managed_rule_group_statement {
          vendor_name = "AWS"
          name        = rule.value
          # Incident reviews and notes can exceed the rule's 8 KB body limit; the apps cap every field
          dynamic "rule_action_override" {
            for_each = rule.value == "AWSManagedRulesCommonRuleSet" ? ["SizeRestrictions_BODY"] : []
            content {
              name = rule_action_override.value
              action_to_use {
                count {}
              }
            }
          }
        }
      }
      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = "edge-${rule.value}"
        sampled_requests_enabled   = true
      }
    }
  }

  # The mobile app is a non-browser HTTP client by nature, so those two
  # signals only count; scrapers, scanners and known bad bots are still blocked
  rule {
    name     = "AWSManagedRulesBotControlRuleSet"
    priority = 10
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesBotControlRuleSet"
        managed_rule_group_configs {
          aws_managed_rules_bot_control_rule_set {
            inspection_level = "COMMON"
          }
        }
        rule_action_override {
          name = "CategoryHttpLibrary"
          action_to_use {
            count {}
          }
        }
        rule_action_override {
          name = "SignalNonBrowserUserAgent"
          action_to_use {
            count {}
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "edge-bot-control"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "rate-limit-per-ip"
    priority = 20
    action {
      block {
        custom_response {
          response_code            = 429
          custom_response_body_key = "blocked"
        }
      }
    }
    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit
        aggregate_key_type = "IP"
        # Every mobile app user reaches the services through the mobile API Lambda, i.e. from a few shared
        # addresses: its requests (X-Mobile-Gateway, a secret only it has) are not counted per IP here.
        # API Gateway throttles the mobile API itself (modules/mobile-api).
        scope_down_statement {
          not_statement {
            statement {
              byte_match_statement {
                search_string         = var.mobile_gateway_secret
                positional_constraint = "EXACTLY"
                field_to_match {
                  single_header {
                    name = "x-mobile-gateway"
                  }
                }
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "edge-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "block-scripted-sign-in"
    priority = 30
    action {
      block {
        custom_response {
          response_code            = 403
          custom_response_body_key = "blocked"
        }
      }
    }
    statement {
      and_statement {
        statement {
          regex_pattern_set_reference_statement {
            arn = aws_wafv2_regex_pattern_set.sign_in_paths.arn
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "LOWERCASE"
            }
          }
        }
        statement {
          regex_pattern_set_reference_statement {
            arn = aws_wafv2_regex_pattern_set.scripted_clients.arn
            field_to_match {
              single_header {
                name = "user-agent"
              }
            }
            text_transformation {
              priority = 0
              type     = "LOWERCASE"
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "edge-scripted-sign-in"
      sampled_requests_enabled   = true
    }
  }

  custom_response_body {
    key          = "blocked"
    content      = "{\"error\":\"request blocked by the web application firewall\"}"
    content_type = "APPLICATION_JSON"
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.name}-edge"
    sampled_requests_enabled   = true
  }
}

# Log group names for WAF must start with aws-waf-logs-, in the web ACL's region
resource "aws_cloudwatch_log_group" "waf" {
  name              = "aws-waf-logs-${var.name}-edge"
  retention_in_days = 30
}

resource "aws_wafv2_web_acl_logging_configuration" "this" {
  resource_arn            = aws_wafv2_web_acl.this.arn
  log_destination_configs = [aws_cloudwatch_log_group.waf.arn]

  # Only what WAF blocked or counted: allowed requests are in the apps' own logs and traces
  logging_filter {
    default_behavior = "DROP"
    filter {
      behavior    = "KEEP"
      requirement = "MEETS_ANY"
      condition {
        action_condition {
          action = "BLOCK"
        }
      }
      condition {
        action_condition {
          action = "COUNT"
        }
      }
    }
  }

  redacted_fields {
    single_header {
      name = "authorization" # bearer tokens of the mobile app
    }
  }
  redacted_fields {
    single_header {
      name = "cookie" # session cookies (signed tokens)
    }
  }
  redacted_fields {
    single_header {
      name = "x-mobile-gateway"
    }
  }
}

resource "aws_wafv2_web_acl_association" "alb" {
  resource_arn = aws_lb.web.arn
  web_acl_arn  = aws_wafv2_web_acl.this.arn
}

resource "aws_wafv2_web_acl_association" "api" {
  resource_arn = var.api_stage_arn
  web_acl_arn  = aws_wafv2_web_acl.this.arn
}
