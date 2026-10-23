# ---------------------------------------------------------------------------
# The origin of the EKS apps: one ALB in the public subnets that only CloudFront can use
#   - its security group admits HTTPS from CloudFront's origin-facing address ranges only (AWS managed prefix list)
#   - every listener rule also requires X-Origin-Verify (a secret only CloudFront sends): another CloudFront
#     distribution, or anyone who finds the ALB, gets a 403
#   - host-based rules, one target group each: agripulse.<zone> and incidents.<zone> -> oauth2-proxy (Cognito
#     staff sign-in) in front of each app; argocd.<zone> -> Argo CD and grafana.<zone> -> Grafana (their own
#     sign-in: Argo CD admin, Grafana admin from Secrets Manager)
# Terraform owns the ALB, its listener, rules, certificate and target groups. Inside EKS the AWS Load Balancer
# Controller only keeps each target group filled with pod IPs (TargetGroupBindings: one in each app chart, the
# platform tools' in infra/modules/k8s-edge), so destroying the cluster never leaves an orphaned load balancer.
# ---------------------------------------------------------------------------

data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

locals {
  # Priorities are fixed: a rule whose priority changes is replaced, so new hosts take new numbers
  alb_apps = {
    incidents = { host = local.hosts["incidents"], port = 4180, health = "/ping", priority = 10 } # oauth2-proxy
    web       = { host = local.hosts["web"], port = 4180, health = "/ping", priority = 11 }       # oauth2-proxy
    argocd    = { host = local.hosts["argocd"], port = 8080, health = "/healthz", priority = 12 } # argocd-server
    grafana   = { host = local.hosts["grafana"], port = 3000, health = "/api/health", priority = 13 }
  }
  pod_ports = toset([for a in values(local.alb_apps) : tostring(a.port)])
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

# One rule per pod port: 4180 oauth2-proxy, 8080 argocd-server, 3000 Grafana
resource "aws_vpc_security_group_egress_rule" "alb_to_pods" {
  for_each                     = local.pod_ports
  security_group_id            = aws_security_group.alb.id
  description                  = "ALB to pods on ${each.key}"
  referenced_security_group_id = var.cluster_security_group_id
  from_port                    = tonumber(each.key)
  to_port                      = tonumber(each.key)
  ip_protocol                  = "tcp"
}

# Pods use the nodes' security group (the cluster security group): admit the ALB on those ports only
resource "aws_vpc_security_group_ingress_rule" "pods_from_alb" {
  for_each                     = local.pod_ports
  security_group_id            = var.cluster_security_group_id
  description                  = "ALB to pods on ${each.key}"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = tonumber(each.key)
  to_port                      = tonumber(each.key)
  ip_protocol                  = "tcp"
}

# The 4180 rules existed before the per-port rules: keep them (a new rule beside the old one would be a duplicate)
moved {
  from = aws_vpc_security_group_egress_rule.alb_to_pods
  to   = aws_vpc_security_group_egress_rule.alb_to_pods["4180"]
}

moved {
  from = aws_vpc_security_group_ingress_rule.pods_from_alb
  to   = aws_vpc_security_group_ingress_rule.pods_from_alb["4180"]
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
  port                 = each.value.port
  protocol             = "HTTP"
  target_type          = "ip" # pod IPs, registered by the load balancer controller
  vpc_id               = var.vpc_id
  deregistration_delay = 30

  health_check {
    path                = each.value.health # health endpoints that need no sign-in
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
  priority     = each.value.priority

  condition {
    host_header {
      values = [each.value.host]
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
