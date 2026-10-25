# ---------------------------------------------------------------------------
# ALL alerting is here, in the observability stack: Prometheus evaluates these rules and Alertmanager sends every
# alert to Slack (critical/warning also open an incident in the incident blog). Three sources:
#
#   OpenTelemetry   span metrics that Tempo derives from the apps' traces (rate, errors, duration per service and
#                   operation): what users of the web app and the incident blog experience, and their calls to
#                   PostgreSQL and Redis
#   AWS             CloudWatch metrics of the ALB, API Gateway, Lambda and RDS, pulled by YACE (below): the edge,
#                   the mobile API and the database
#   AIOps           anomaly rules compare a metric with its own history (7-day mean + 3 standard deviations, or the
#                   same time last week) instead of a fixed number
# The apps' own rules (cache hit ratio, canary aborted, SEV1 not triaged...) ship with their Helm charts.
# CloudWatch data arrives a few minutes late, so the AWS rules wait longer before they fire.
# ---------------------------------------------------------------------------

locals {
  aws = { namespace = "aws" } # Slack titles show "(aws, <severity>)"

  # 3-sigma anomaly of an expression against its own last 7 days (a subquery, so it also works on aggregations)
  anomaly = "> (avg_over_time((%s)[7d:5m]) + 3 * stddev_over_time((%s)[7d:5m]))"
  # the span metrics' k8s_namespace_name (<service>-<environment>) as "namespace": staging alerts only go to Slack
  ns = "label_replace(%s, \"namespace\", \"$1\", \"k8s_namespace_name\", \"(.*)\")"

  alert_rules = {
    agripulse-opentelemetry = {
      groups = [{
        name = "agripulse-opentelemetry"
        rules = [
          {
            alert       = "AgriPulseServiceErrorRate"
            expr        = <<-EOT
              label_replace(
                sum by (service, k8s_namespace_name) (rate(traces_spanmetrics_calls_total{span_kind="SPAN_KIND_SERVER", status_code="STATUS_CODE_ERROR"}[5m]))
                  / clamp_min(sum by (service, k8s_namespace_name) (rate(traces_spanmetrics_calls_total{span_kind="SPAN_KIND_SERVER"}[5m])), 1e-6) > 0.05,
                "namespace", "$1", "k8s_namespace_name", "(.*)")
            EOT
            for         = "5m"
            labels      = { severity = "critical" }
            annotations = { summary = "{{ $labels.service }} ({{ $labels.namespace }}): {{ $value | humanizePercentage }} of requests fail (OpenTelemetry traces). Grafana > Tempo: search status=error for this service." }
          },
          {
            alert       = "AgriPulseServiceSlow"
            expr        = format("%s > 1", format(local.ns, "histogram_quantile(0.95, sum by (le, service, k8s_namespace_name) (rate(traces_spanmetrics_latency_bucket{span_kind=\"SPAN_KIND_SERVER\"}[5m])))"))
            for         = "10m"
            labels      = { severity = "warning" }
            annotations = { summary = "{{ $labels.service }} ({{ $labels.namespace }}): p95 request latency {{ $value | humanizeDuration }} (OpenTelemetry traces); open a slow trace in Grafana from the latency panel." }
          },
          {
            # Client spans: the apps' calls to PostgreSQL (SQLAlchemy) and Valkey (Redis)
            alert       = "AgriPulseDependencySlow"
            expr        = format("%s > 0.5", format(local.ns, "histogram_quantile(0.95, sum by (le, service, span_name, k8s_namespace_name) (rate(traces_spanmetrics_latency_bucket{span_kind=\"SPAN_KIND_CLIENT\"}[5m])))"))
            for         = "10m"
            labels      = { severity = "warning" }
            annotations = { summary = "{{ $labels.service }} ({{ $labels.namespace }}): p95 of '{{ $labels.span_name }}' is {{ $value | humanizeDuration }} (database, cache or service call)." }
          },
          {
            alert       = "AgriPulseLatencyAnomaly"
            expr        = format("service:request_p95:10m %s and service:request_p95:10m > 0.3", format(local.anomaly, "service:request_p95:10m", "service:request_p95:10m"))
            for         = "15m"
            labels      = { severity = "warning" }
            annotations = { summary = "{{ $labels.service }} ({{ $labels.namespace }}): p95 latency is far above its usual level for the last 7 days (AIOps anomaly)." }
          },
          {
            record = "service:request_p95:10m"
            expr   = format(local.ns, "histogram_quantile(0.95, sum by (le, service, k8s_namespace_name) (rate(traces_spanmetrics_latency_bucket{span_kind=\"SPAN_KIND_SERVER\"}[10m])))")
          },
          {
            alert       = "AgriPulseTelemetryMissing"
            expr        = "sum(rate(otelcol_receiver_accepted_spans_total[15m])) == 0 or absent(otelcol_receiver_accepted_spans_total)"
            for         = "15m"
            labels      = { severity = "warning", namespace = "opentelemetry" }
            annotations = { summary = "The OpenTelemetry Collector received no spans for 15 minutes: the apps are down, idle, or cannot reach the collector (alerts above then go blind)." }
          },
        ]
      }]
    }

    agripulse-aws = {
      groups = [{
        name     = "agripulse-aws"
        interval = "1m"
        rules = [
          # ---- edge: ALB in front of the web app and the incident blog ----
          {
            alert       = "AgriPulseAlbTarget5xx"
            expr        = "sum(aws_applicationelb_httpcode_target_5_xx_count_sum) > 5"
            for         = "5m"
            labels      = merge(local.aws, { severity = "critical" })
            annotations = { summary = "The web app or the incident blog returned {{ $value }} 5xx in 5 minutes (ALB)." }
          },
          {
            alert       = "AgriPulseNoHealthyTargets"
            expr        = "min by (dimension_TargetGroup) (aws_applicationelb_healthy_host_count_minimum) < 1"
            for         = "10m"
            labels      = merge(local.aws, { severity = "critical" })
            annotations = { summary = "No healthy pod in {{ $labels.dimension_TargetGroup }}: that site is down." }
          },
          {
            alert       = "AgriPulseAlbLatencyAnomaly"
            expr        = format("max(aws_applicationelb_target_response_time_p90) %s and max(aws_applicationelb_target_response_time_p90) > 0.5", format(local.anomaly, "max(aws_applicationelb_target_response_time_p90)", "max(aws_applicationelb_target_response_time_p90)"))
            for         = "15m"
            labels      = merge(local.aws, { severity = "warning" })
            annotations = { summary = "ALB p90 response time {{ $value | humanizeDuration }} is far above its 7-day pattern (AIOps anomaly)." }
          },
          {
            alert       = "AgriPulseAlbTrafficDrop"
            expr        = "sum(aws_applicationelb_request_count_sum) < 0.3 * sum(aws_applicationelb_request_count_sum offset 1w) and sum(aws_applicationelb_request_count_sum offset 1w) > 100"
            for         = "30m"
            labels      = merge(local.aws, { severity = "warning" })
            annotations = { summary = "Web traffic is below 30 % of the same time last week (sign-in broken, DNS, CloudFront?)." }
          },
          # ---- mobile API: API Gateway + Lambda ----
          {
            alert       = "AgriPulseApi5xx"
            expr        = "sum(aws_apigateway_5_xxerror_sum) > 5"
            for         = "5m"
            labels      = merge(local.aws, { severity = "critical" })
            annotations = { summary = "The mobile API returned {{ $value }} 5xx in 5 minutes (API Gateway)." }
          },
          {
            alert       = "AgriPulseApiLatencyAnomaly"
            expr        = format("max(aws_apigateway_latency_p90) %s and max(aws_apigateway_latency_p90) > 500", format(local.anomaly, "max(aws_apigateway_latency_p90)", "max(aws_apigateway_latency_p90)"))
            for         = "15m"
            labels      = merge(local.aws, { severity = "warning" })
            annotations = { summary = "Mobile API p90 latency {{ $value }} ms is far above its 7-day pattern (AIOps anomaly)." }
          },
          {
            alert       = "AgriPulseApiTrafficDrop"
            expr        = "sum(aws_apigateway_count_sum) < 0.3 * sum(aws_apigateway_count_sum offset 1w) and sum(aws_apigateway_count_sum offset 1w) > 100"
            for         = "30m"
            labels      = merge(local.aws, { severity = "warning" })
            annotations = { summary = "Mobile API traffic is below 30 % of the same time last week (app release broke sign-in, DNS?)." }
          },
          {
            alert       = "AgriPulseLambdaErrors"
            expr        = "sum(aws_lambda_errors_sum{dimension_FunctionName=~\".*mobile-api.*\"}) > 0"
            for         = "5m"
            labels      = merge(local.aws, { severity = "warning" })
            annotations = { summary = "The mobile API Lambda raised {{ $value }} errors (the canary rolls back by itself during a deployment)." }
          },
          {
            alert       = "AgriPulseLambdaThrottles"
            expr        = "sum(aws_lambda_throttles_sum) > 0"
            for         = "5m"
            labels      = merge(local.aws, { severity = "warning" })
            annotations = { summary = "Lambda is throttled: the concurrency limit (the cap on PostgreSQL connections) is reached." }
          },
          # ---- PostgreSQL ----
          {
            alert       = "AgriPulsePostgresCpuAnomaly"
            expr        = format("max(aws_rds_cpuutilization_average) %s and max(aws_rds_cpuutilization_average) > 40", format(local.anomaly, "max(aws_rds_cpuutilization_average)", "max(aws_rds_cpuutilization_average)"))
            for         = "15m"
            labels      = merge(local.aws, { severity = "warning" })
            annotations = { summary = "PostgreSQL CPU {{ $value | humanize }} % is far above its 7-day pattern (check the Redis hit ratio and slow queries)." }
          },
          {
            alert       = "AgriPulsePostgresCpuCreditsLow"
            expr        = "min(aws_rds_cpucredit_balance_minimum) < 20"
            for         = "15m"
            labels      = merge(local.aws, { severity = "warning" })
            annotations = { summary = "db.t3.micro is running out of CPU credits ({{ $value }} left): it will be throttled to its baseline." }
          },
          {
            alert       = "AgriPulsePostgresConnectionsHigh"
            expr        = "max(aws_rds_database_connections_maximum) > 65"
            for         = "10m"
            labels      = merge(local.aws, { severity = "warning" })
            annotations = { summary = "{{ $value }} PostgreSQL connections, near the db.t3.micro limit (~80)." }
          },
          {
            alert       = "AgriPulsePostgresStorageLow"
            expr        = "min(aws_rds_free_storage_space_minimum) < 2147483648"
            for         = "15m"
            labels      = merge(local.aws, { severity = "critical" })
            annotations = { summary = "PostgreSQL has {{ $value | humanize1024 }}B free storage left." }
          },
          {
            alert       = "AgriPulseCloudWatchMetricsMissing"
            expr        = "absent(aws_rds_cpuutilization_average) or absent(aws_applicationelb_request_count_sum)"
            for         = "30m"
            labels      = merge(local.aws, { severity = "warning" })
            annotations = { summary = "YACE is not delivering CloudWatch metrics: the AWS alerts above cannot fire (check the yace pod and its Pod Identity role)." }
          },
        ]
      }]
    }
  }
}

# ---------------- YACE: CloudWatch -> Prometheus ----------------
# Finds the project's resources by their tag (Project = agripulse, set on everything by Terraform) and exports their
# metrics as aws_<namespace>_<metric>_<statistic>, e.g. aws_rds_cpuutilization_average. AWS rights: the Pod Identity
# role of the ServiceAccount "yace" (infra/aws/main.tf), read-only.
locals {
  yace_job = {
    for type, metrics in {
      "AWS/ApplicationELB" = [["HTTPCode_Target_5XX_Count", "Sum"], ["RequestCount", "Sum"], ["TargetResponseTime", "p90"], ["HealthyHostCount", "Minimum"]]
      "AWS/ApiGateway"     = [["5XXError", "Sum"], ["Count", "Sum"], ["Latency", "p90"]]
      "AWS/Lambda"         = [["Errors", "Sum"], ["Throttles", "Sum"], ["Invocations", "Sum"]]
      "AWS/RDS"            = [["CPUUtilization", "Average"], ["CPUCreditBalance", "Minimum"], ["DatabaseConnections", "Maximum"], ["FreeStorageSpace", "Minimum"]]
      } : type => {
      type       = type
      regions    = [var.region]
      searchTags = [{ key = "Project", value = "^${var.project_tag}$" }]
      period     = 300
      length     = 600
      nilToZero  = true
      metrics    = [for m in metrics : { name = m[0], statistics = [m[1]] }]
    }
  }
}

resource "helm_release" "yace" {
  name       = "yace"
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "prometheus-yet-another-cloudwatch-exporter"
  version    = "0.48.0" # YACE v0.68.0
  namespace  = kubernetes_namespace_v1.monitoring.metadata[0].name

  values = [yamlencode({
    serviceAccount = { create = true, name = "yace" } # Pod Identity association by this name
    serviceMonitor = { enabled = true, interval = "60s", timeout = "50s" }
    resources      = { requests = { cpu = "20m", memory = "64Mi" }, limits = { memory = "256Mi" } }
    # The image's USER is "nobody", a name: with runAsNonRoot the kubelet cannot prove a name is not root and
    # refuses to start the container, so the numeric id (nobody = 65534) is required
    securityContext = { readOnlyRootFilesystem = true, allowPrivilegeEscalation = false, runAsNonRoot = true,
    runAsUser = 65534, runAsGroup = 65534, capabilities = { drop = ["ALL"] } }
    # The role is read-only CloudWatch/tagging: no iam:ListAccountAliases (the account_alias label stays empty)
    extraArgs = { "disable-account-alias-lookup" = "true" }
    config = yamlencode({
      apiVersion = "v1alpha1"
      sts-region = var.region
      discovery  = { jobs = values(local.yace_job) }
    })
  })]

  depends_on = [helm_release.monitoring] # ServiceMonitor CRD
}
