# ---------------------------------------------------------------------------
# Mobile API alarms -> the alarm topic (modules/monitoring: e-mail, and the incident blog)
#   static     5xx, errors on the live alias, throttles
#   anomaly    CloudWatch anomaly detection (machine learning, AIOps): latency above, traffic below the band
#              learned per hour of day and day of week
# The "live errors" alarm is the gate the pipeline's canary checks before shifting 100 % of traffic.
# ---------------------------------------------------------------------------

locals {
  api_dims = { ApiName = aws_api_gateway_rest_api.mobile.name, Stage = aws_api_gateway_stage.prod.stage_name }
}

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${var.name}-api-5xx"
  alarm_description   = "Mobile API returns 5xx errors"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5XXError"
  dimensions          = local.api_dims
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alarm_topic_arn]
  ok_actions          = [var.alarm_topic_arn]
}

# Gate for the pipeline's canary: any error on the live alias during the bake time
resource "aws_cloudwatch_metric_alarm" "lambda_live_errors" {
  alarm_name          = "${var.name}-lambda-live-errors"
  alarm_description   = "Errors on the live alias (checked by the canary deployment before shifting traffic)"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.api.function_name, Resource = "${aws_lambda_function.api.function_name}:${aws_lambda_alias.live.name}" }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alarm_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  alarm_name          = "${var.name}-lambda-throttles"
  alarm_description   = "The mobile API hit its reserved concurrency (the cap that protects PostgreSQL's connections)"
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions          = { FunctionName = aws_lambda_function.api.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alarm_topic_arn]
}

# AIOps: latency outside its learned normal band
resource "aws_cloudwatch_metric_alarm" "api_latency_anomaly" {
  alarm_name          = "${var.name}-api-latency-anomaly"
  alarm_description   = "Mobile API p90 latency is above the band learned by CloudWatch anomaly detection"
  comparison_operator = "GreaterThanUpperThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold_metric_id = "band"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alarm_topic_arn]

  metric_query {
    id          = "latency"
    return_data = true
    metric {
      namespace   = "AWS/ApiGateway"
      metric_name = "Latency"
      dimensions  = local.api_dims
      stat        = "p90"
      period      = 300
    }
  }

  metric_query {
    id          = "band"
    expression  = "ANOMALY_DETECTION_BAND(latency, 2)"
    label       = "Latency (expected band)"
    return_data = true
  }
}

# AIOps: request volume collapsing (an app release broke sign-in, DNS, a regional issue)
resource "aws_cloudwatch_metric_alarm" "api_traffic_drop_anomaly" {
  alarm_name          = "${var.name}-api-traffic-drop-anomaly"
  alarm_description   = "Mobile API request count fell below the learned band"
  comparison_operator = "LessThanLowerThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold_metric_id = "band"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alarm_topic_arn]

  metric_query {
    id          = "requests"
    return_data = true
    metric {
      namespace   = "AWS/ApiGateway"
      metric_name = "Count"
      dimensions  = local.api_dims
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
