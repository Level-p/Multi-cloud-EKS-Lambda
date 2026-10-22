# ---------------------------------------------------------------------------
# The origin of the EKS apps: one ALB in the public subnets that only CloudFront can use
#   - its security group admits HTTPS from CloudFront's origin-facing address ranges only (AWS managed prefix list)
#   - every listener rule also requires X-Origin-Verify (a secret only CloudFront sends): another CloudFront
#     distribution, or anyone who finds the ALB, gets a 403
#   - host-based rules: agripulse.<zone> -> target group "web", incidents.<zone> -> target group "incidents"
# Terraform owns the ALB, its listener, rules, certificate and target groups. Inside EKS the AWS Load Balancer
# Controller only keeps each target group filled with the oauth2-proxy pod IPs (a TargetGroupBinding in each
# chart), so destroying the cluster never leaves an orphaned load balancer.
# ---------------------------------------------------------------------------

data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

locals {
  alb_apps = { web = local.hosts["web"], incidents = local.hosts["incidents"] }
}

resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "HTTPS from CloudFront only; the ALB reaches only oauth2-proxy pods"
  vpc_id      = var.vpc_id
  tags        = { Name = "${var.name}-alb" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from the CloudFront origin-facing servers" # EC2 rejects apostrophes here
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_pods" {
  security_group_id            = aws_security_group.alb.id
  description                  = "oauth2-proxy pods"
  referenced_security_group_id = var.cluster_security_group_id
  from_port                    = 4180
  to_port                      = 4180
  ip_protocol                  = "tcp"
}

# Pods use the nodes' security group (the cluster security group): admit the ALB on oauth2-proxy's port only
resource "aws_vpc_security_group_ingress_rule" "pods_from_alb" {
  security_group_id            = var.cluster_security_group_id
  description                  = "ALB to oauth2-proxy pods"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 4180
  to_port                      = 4180
  ip_protocol                  = "tcp"
}

resource "aws_lb" "web" {
  name                       = "${var.name}-web"
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [aws_security_group.alb.id]
  subnets                    = var.public_subnet_ids
  drop_invalid_header_fields = true # no request smuggling through malformed headers
  enable_deletion_protection = var.deletion_protection
  idle_timeout               = 60
}

resource "aws_lb_target_group" "app" {
  for_each             = local.alb_apps
  name                 = "${var.name}-${each.key}"
  port                 = 4180
  protocol             = "HTTP"
  target_type          = "ip" # pod IPs, registered by the load balancer controller
  vpc_id               = var.vpc_id
  deregistration_delay = 30

  health_check {
    path                = "/ping" # oauth2-proxy's own health endpoint (no sign-in needed)
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.web.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.alb.certificate_arn

  # Without the origin header, or for any other host name: 403
  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "application/json"
      message_body = "{\"error\":\"forbidden\"}"
      status_code  = "403"
    }
  }
}

resource "aws_lb_listener_rule" "app" {
  for_each     = local.alb_apps
  listener_arn = aws_lb_listener.https.arn
  priority     = index(keys(local.alb_apps), each.key) + 10

  condition {
    host_header {
      values = [each.value]
    }
  }

  condition {
    http_header {
      http_header_name = "X-Origin-Verify"
      values           = [var.origin_verify_secret]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app[each.key].arn
  }
}
