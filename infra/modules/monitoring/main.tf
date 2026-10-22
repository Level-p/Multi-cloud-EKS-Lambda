# ---------------------------------------------------------------------------
# Alarms and where they go
#   SNS topic "<name>-alarms"  -> e-mail (optional) and the forwarder Lambda -> the incident blog, which opens
#                                 an incident (POST /webhooks/cloudwatch with its Bearer token)
#   static                     ALB 5xx, no healthy oauth2-proxy pods, RDS storage/connections, malware found
#   anomaly (AIOps)            CloudWatch anomaly detection learns each metric's band per hour of day and day of
#                              week and alarms outside it: ALB latency and traffic, RDS CPU
# The mobile API's own alarms are in modules/mobile-api; Prometheus/Alertmanager watch inside EKS.
# ---------------------------------------------------------------------------

data "aws_partition" "current" {}

resource "aws_sns_topic" "alarms" {
  name              = "${var.name}-alarms"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alarm_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

resource "aws_sns_topic_policy" "alarms" {
  arn = aws_sns_topic.alarms.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "CloudWatchAlarms", Effect = "Allow", Principal = { Service = "cloudwatch.amazonaws.com" }, Action = "sns:Publish", Resource = aws_sns_topic.alarms.arn },
      { Sid = "EventBridge", Effect = "Allow", Principal = { Service = "events.amazonaws.com" }, Action = "sns:Publish", Resource = aws_sns_topic.alarms.arn },
    ]
  })
}

# ---------------- forwarder: alarm -> incident blog ----------------
data "archive_file" "forwarder" {
  type        = "zip"
  source_file = "${path.module}/forwarder/handler.py"
  output_path = "${path.root}/.build/alarm-forwarder.zip"
}

resource "aws_cloudwatch_log_group" "forwarder" {
  name              = "/aws/lambda/${var.name}-alarm-forwarder"
  retention_in_days = 30
}

resource "aws_iam_role" "forwarder" {
  name = "${var.name}-alarm-forwarder"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "forwarder" {
  name = "forward-alarms"
  role = aws_iam_role.forwarder.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = var.webhook_token_secret_arn },
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.forwarder.arn}:*" },
    ]
  })
}

resource "aws_lambda_function" "forwarder" {
  #checkov:skip=CKV_AWS_117:Calls the public incident blog URL and Secrets Manager: no VPC needed
  #checkov:skip=CKV_AWS_116:SNS retries failed deliveries itself
  #checkov:skip=CKV_AWS_272:Code signing is not used for this small in-repo function
  function_name    = "${var.name}-alarm-forwarder"
  role             = aws_iam_role.forwarder.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.forwarder.output_path
  source_code_hash = data.archive_file.forwarder.output_base64sha256
  timeout          = 15
  # no reserved concurrency: the account quota may be the minimum (10, all unreserved)

  environment {
    variables = {
      WEBHOOK_URL              = var.webhook_url
      WEBHOOK_TOKEN_SECRET_ARN = var.webhook_token_secret_arn
    }
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.forwarder.name
  }

  tracing_config {
    mode = "Active"
  }
}

resource "aws_lambda_permission" "sns" {
  statement_id  = "AllowAlarmTopic"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.forwarder.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alarms.arn
}

resource "aws_sns_topic_subscription" "forwarder" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.forwarder.arn
}

# ---------------- ALB (web app, incident blog) ----------------
locals {
  alb_dims = { LoadBalancer = var.alb_arn_suffix }
}

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${var.name}-alb-target-5xx"
  alarm_description   = "The web app or the incident blog returns 5xx errors"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = local.alb_dims
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_targets" {
  for_each            = var.target_group_arn_suffixes
  alarm_name          = "${var.name}-${each.key}-no-healthy-targets"
  alarm_description   = "No healthy oauth2-proxy pod in the ${each.key} target group"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  dimensions          = { LoadBalancer = var.alb_arn_suffix, TargetGroup = each.value }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "alb_latency_anomaly" {
  alarm_name          = "${var.name}-alb-latency-anomaly"
  alarm_description   = "ALB target p90 response time is above the band learned by CloudWatch anomaly detection"
  comparison_operator = "GreaterThanUpperThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold_metric_id = "band"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]

  metric_query {
    id          = "latency"
    return_data = true
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "TargetResponseTime"
      dimensions  = local.alb_dims
      stat        = "p90"
      period      = 300
    }
  }

  metric_query {
    id          = "band"
    expression  = "ANOMALY_DETECTION_BAND(latency, 2)"
    label       = "Response time (expected band)"
    return_data = true
  }
}

resource "aws_cloudwatch_metric_alarm" "alb_traffic_drop_anomaly" {
  alarm_name          = "${var.name}-alb-traffic-drop-anomaly"
  alarm_description   = "ALB request count fell below the learned band (sign-in broken, DNS, CloudFront)"
  comparison_operator = "LessThanLowerThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold_metric_id = "band"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]

  metric_query {
    id          = "requests"
    return_data = true
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "RequestCount"
      dimensions  = local.alb_dims
      stat        = "Sum"
      period      = 300
    }
  }

  metric_query {
    id          = "band"
    expression  = "ANOMALY_DETECTION_BAND(requests, 3)"
    label       = "Requests (expected band)"
    return_data = true
  }
}

# ---------------- RDS PostgreSQL ----------------
locals {
  db_dims = { DBInstanceIdentifier = var.db_identifier }
}

# AIOps: CPU outside its learned pattern (missing index, runaway query, the Redis cache down)
resource "aws_cloudwatch_metric_alarm" "db_cpu_anomaly" {
  alarm_name          = "${var.name}-postgres-cpu-anomaly"
  alarm_description   = "PostgreSQL CPU is above the learned band (check the Redis hit ratio and slow queries first)"
  comparison_operator = "GreaterThanUpperThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold_metric_id = "band"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]

  metric_query {
    id          = "cpu"
    return_data = true
    metric {
      namespace   = "AWS/RDS"
      metric_name = "CPUUtilization"
      dimensions  = local.db_dims
      stat        = "Average"
      period      = 300
    }
  }

  metric_query {
    id          = "band"
    expression  = "ANOMALY_DETECTION_BAND(cpu, 2)"
    label       = "CPU (expected band)"
    return_data = true
  }
}

# db.t3.micro: CPU credits run out under sustained load, then the instance is throttled to its baseline
resource "aws_cloudwatch_metric_alarm" "db_cpu_credits" {
  alarm_name          = "${var.name}-postgres-cpu-credits-low"
  alarm_description   = "PostgreSQL (burstable) is running out of CPU credits: move to a larger class or find the load"
  namespace           = "AWS/RDS"
  metric_name         = "CPUCreditBalance"
  dimensions          = local.db_dims
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 3
  threshold           = 20
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "db_connections" {
  alarm_name          = "${var.name}-postgres-connections-high"
  alarm_description   = "PostgreSQL connections near the db.t3.micro limit (~80): Lambda concurrency or pod pools too high"
  namespace           = "AWS/RDS"
  metric_name         = "DatabaseConnections"
  dimensions          = local.db_dims
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  threshold           = var.db_connection_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "db_storage" {
  alarm_name          = "${var.name}-postgres-free-storage-low"
  alarm_description   = "Less than 2 GiB free (storage autoscaling grows it, up to its maximum)"
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  dimensions          = local.db_dims
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 2147483648
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

# Multi-AZ failover or other availability events of the instance
resource "aws_db_event_subscription" "this" {
  name             = "${var.name}-postgres-events"
  sns_topic        = aws_sns_topic.alarms.arn
  source_type      = "db-instance"
  source_ids       = [var.db_identifier]
  event_categories = ["failover", "failure", "low storage", "maintenance"]
}

# ---------------- GuardDuty found malware in an upload: security incident ----------------
resource "aws_cloudwatch_event_rule" "malware_found" {
  name        = "${var.name}-malware-found"
  description = "GuardDuty Malware Protection for S3 found a threat in a farmer upload"
  event_pattern = jsonencode({
    source        = ["aws.guardduty"]
    "detail-type" = ["GuardDuty Malware Protection Object Scan Result"]
    detail = {
      scanStatus        = ["COMPLETED"]
      scanResultDetails = { scanResultStatus = ["THREATS_FOUND"] }
    }
  })
}

resource "aws_cloudwatch_event_target" "malware_found" {
  rule = aws_cloudwatch_event_rule.malware_found.name
  arn  = aws_sns_topic.alarms.arn
}
