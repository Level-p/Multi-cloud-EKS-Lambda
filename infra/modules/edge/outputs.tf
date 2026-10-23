output "hosts" {
  value = local.hosts
}

output "target_group_arns" {
  description = "Target group per host: a TargetGroupBinding fills it with pod IPs (app charts, k8s-edge)"
  value       = { for k, tg in aws_lb_target_group.app : k => tg.arn }
}

output "target_group_arn_suffixes" {
  value = { for k, tg in aws_lb_target_group.app : k => tg.arn_suffix }
}

output "alb_arn_suffix" {
  value = aws_lb.web.arn_suffix
}

output "web_acl_arn" {
  value = aws_wafv2_web_acl.this.arn
}

output "distribution_ids" {
  value = { app = aws_cloudfront_distribution.app.id, api = aws_cloudfront_distribution.api.id, media = aws_cloudfront_distribution.media.id }
}
