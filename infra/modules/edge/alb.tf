# ---------------------------------------------------------------------------
# The public entrance of the EKS apps: one internet-facing ALB in the public subnets, with the WAF (waf.tf)
#   - HTTPS on 443 (TLS 1.2+); port 80 only redirects to HTTPS
#   - host-based rules, one target group each: every service in each environment (shop.<zone>, shop.staging.<zone>,
#     ...: AgriPulse accounts, signed tokens checked by the services), argocd.<zone> and grafana.<zone> (their own
#     sign-in: kubectl get secret, see infra/platform outputs). Any other host name: 404
# Terraform owns the ALB, its listener, rules, certificate and target groups. Inside EKS the AWS Load Balancer
# Controller only keeps each target group filled with pod IPs (TargetGroupBindings: one in each app chart, the
# platform tools' in infra/modules/k8s-edge), so destroying the cluster never leaves an orphaned load balancer.
# ---------------------------------------------------------------------------

locals {
  alb_apps  = var.alb_routes
  pod_ports = toset([for a in values(local.alb_apps) : tostring(a.port)])
}

resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "HTTPS and the HTTP redirect from the internet; the ALB reaches only the service pods"
  vpc_id      = var.vpc_id
  tags        = { Name = "${var.name}-alb" }
}

# Public on purpose: the services, Argo CD and Grafana are reached here directly (the WAF filters every request)
resource "aws_vpc_security_group_ingress_rule" "alb_from_internet" {
  #checkov:skip=CKV_AWS_260:Port 80 only answers with a redirect to HTTPS
  for_each          = toset(["80", "443"])
  security_group_id = aws_security_group.alb.id
  description       = each.key == "443" ? "HTTPS from the internet" : "HTTP from the internet (redirected to HTTPS)" # EC2 rejects apostrophes here
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = tonumber(each.key)
  to_port           = tonumber(each.key)
  ip_protocol       = "tcp"
}

# One rule per pod port: 8080 the services and argocd-server, 3000 Grafana
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

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.web.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      protocol    = "HTTPS"
      port        = "443"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.web.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.regional.certificate_arn

  # Any host name that is not one of ours (e.g. the ALB's own DNS name): 404
  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "application/json"
      message_body = "{\"error\":\"not found\"}"
      status_code  = "404"
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

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app[each.key].arn
  }
}
