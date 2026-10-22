# ---------------------------------------------------------------------------
# Alerting lives in the observability stack inside EKS (infra/modules/k8s-observability): OpenTelemetry span
# metrics, the apps' /metrics and the AWS metrics that YACE pulls from CloudWatch (ALB, API Gateway, Lambda, RDS)
# are evaluated by Prometheus, and Alertmanager sends EVERY alert to Slack. No CloudWatch alarm, no SNS topic.
#
# What Prometheus cannot see are one-off AWS EVENTS, not metrics. This module sends those few straight to the same
# Slack channel with an EventBridge API destination (no SNS, no Lambda):
#   GuardDuty Malware Protection   a farmer upload contains malware
#   RDS                            failover, failure, low storage, maintenance of the PostgreSQL instance
# ---------------------------------------------------------------------------

# Slack's incoming webhook carries its secret in the URL; EventBridge still requires a connection, so it gets a
# harmless API-key header (Slack ignores it)
resource "aws_cloudwatch_event_connection" "slack" {
  name               = "${var.name}-slack"
  description        = "Slack incoming webhook (the secret is in the URL)"
  authorization_type = "API_KEY"
  auth_parameters {
    api_key {
      key   = "X-Agripulse-Source"
      value = "eventbridge"
    }
  }
}

resource "aws_cloudwatch_event_api_destination" "slack" {
  name                             = "${var.name}-slack"
  description                      = "The project's one alert channel"
  invocation_endpoint              = var.slack_webhook_url
  http_method                      = "POST"
  invocation_rate_limit_per_second = 5
  connection_arn                   = aws_cloudwatch_event_connection.slack.arn
}

resource "aws_iam_role" "events_to_slack" {
  name = "${var.name}-events-to-slack"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "events.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "events_to_slack" {
  name = "invoke-slack-destination"
  role = aws_iam_role.events_to_slack.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "events:InvokeApiDestination", Resource = aws_cloudwatch_event_api_destination.slack.arn }]
  })
}

locals {
  # event rule => pattern, and the Slack message built from the event (input transformer)
  events = {
    malware-found = {
      description = "GuardDuty Malware Protection for S3 found a threat in a farmer upload"
      pattern = {
        source        = ["aws.guardduty"]
        "detail-type" = ["GuardDuty Malware Protection Object Scan Result"]
        detail        = { scanStatus = ["COMPLETED"], scanResultDetails = { scanResultStatus = ["THREATS_FOUND"] } }
      }
      paths = { bucket = "$.detail.s3ObjectDetails.bucketName", key = "$.detail.s3ObjectDetails.objectKey" }
      text  = ":biohazard_sign: *SECURITY*: GuardDuty found malware in an upload: s3://<bucket>/<key>. It is never served (only clean uploads are). Follow the security incident process."
    }
    postgres-event = {
      description = "RDS event on the PostgreSQL instance (failover, failure, low storage, maintenance)"
      pattern = {
        source        = ["aws.rds"]
        "detail-type" = ["RDS DB Instance Event"]
        detail        = { SourceIdentifier = [var.db_identifier], EventCategories = [["failover"], ["failure"], ["low storage"], ["maintenance"]] }
      }
      paths = { db = "$.detail.SourceIdentifier", message = "$.detail.Message", id = "$.detail.EventID" }
      text  = ":warning: *RDS* <db> (<id>): <message>"
    }
  }
}

resource "aws_cloudwatch_event_rule" "to_slack" {
  for_each      = local.events
  name          = "${var.name}-${each.key}"
  description   = each.value.description
  event_pattern = jsonencode(each.value.pattern)
}

resource "aws_cloudwatch_event_target" "to_slack" {
  for_each = local.events
  rule     = aws_cloudwatch_event_rule.to_slack[each.key].name
  arn      = aws_cloudwatch_event_api_destination.slack.arn
  role_arn = aws_iam_role.events_to_slack.arn

  input_transformer {
    input_paths    = each.value.paths
    input_template = jsonencode({ text = each.value.text })
  }

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 10
  }
}
