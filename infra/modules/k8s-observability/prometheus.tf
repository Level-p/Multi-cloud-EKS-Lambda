# ---------------------------------------------------------------------------
# Prometheus, Alertmanager and Grafana (kube-prometheus-stack)
#   Prometheus      scrapes the apps (/metrics), Valkey, the controllers; Argo Rollouts' canary analysis
#                   queries it; Tempo writes span metrics into it (observability.tf)
#   Alertmanager    EVERY alert -> Slack (the one alert channel); critical/warning also open an incident in the
#                   incident blog
#   Grafana         Prometheus + Tempo + Loki, linked both ways; admin password from Secrets Manager
# EKS runs the control plane itself: etcd, the scheduler, the controller manager (and kube-proxy's
# metrics, bound to localhost) cannot be scraped, so those jobs are off instead of alerting forever.
# ---------------------------------------------------------------------------

locals {
  incident_webhook = "http://incident-blog.incident-response.svc:8080/webhooks/alertmanager"
}

resource "kubernetes_namespace_v1" "monitoring" {
  metadata { name = "monitoring" }
}

data "aws_secretsmanager_secret_version" "webhook_token" {
  secret_id = var.webhook_token_secret_arn
}

data "aws_secretsmanager_secret_version" "grafana_password" {
  secret_id = var.grafana_password_secret_arn
}

resource "kubernetes_secret_v1" "alertmanager_incident_token" {
  metadata {
    name      = "alertmanager-incident-blog"
    namespace = kubernetes_namespace_v1.monitoring.metadata[0].name
  }
  data = { token = data.aws_secretsmanager_secret_version.webhook_token.secret_string }
}

data "aws_secretsmanager_secret_version" "slack_webhook" {
  secret_id = var.slack_webhook_secret_arn
}

resource "kubernetes_secret_v1" "alertmanager_slack" {
  metadata {
    name      = "alertmanager-slack"
    namespace = kubernetes_namespace_v1.monitoring.metadata[0].name
  }
  data = { "webhook-url" = data.aws_secretsmanager_secret_version.slack_webhook.secret_string }
}

resource "kubernetes_secret_v1" "grafana_admin" {
  metadata {
    name      = "grafana-admin"
    namespace = kubernetes_namespace_v1.monitoring.metadata[0].name
  }
  data = { "admin-user" = "admin", "admin-password" = data.aws_secretsmanager_secret_version.grafana_password.secret_string }
}

resource "helm_release" "monitoring" {
  name       = "kube-prometheus-stack"
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  version    = "92.10.1"
  namespace  = kubernetes_namespace_v1.monitoring.metadata[0].name
  timeout    = 900

  values = [yamlencode({
    kubeEtcd              = { enabled = false }
    kubeScheduler         = { enabled = false }
    kubeControllerManager = { enabled = false }
    kubeProxy             = { enabled = false }
    prometheus = {
      prometheusSpec = {
        serviceMonitorSelectorNilUsesHelmValues = false
        ruleSelectorNilUsesHelmValues           = false
        retention                               = "10d"
        enableRemoteWriteReceiver               = true                 # Tempo writes span metrics here (observability.tf)
        enableFeatures                          = ["exemplar-storage"] # keeps the trace ids attached to those metrics
      }
    }
    # The alert rules of the whole project: OpenTelemetry span metrics and AWS (CloudWatch via YACE) (alerts.tf)
    additionalPrometheusRulesMap = local.alert_rules
    alertmanager = {
      alertmanagerSpec = { secrets = ["alertmanager-incident-blog", "alertmanager-slack"] }
      config = {
        route = {
          receiver = "slack" # anything not matched below still reaches Slack
          group_by = ["alertname", "namespace"]
          routes = [
            # Prometheus' own heartbeat alerts are not incidents
            { receiver = "null", matchers = ["alertname =~ \"Watchdog|InfoInhibitor\""] },
            # Critical and warning alerts also open an incident in the incident blog (Detected state)...
            { receiver = "incident-blog", matchers = ["severity =~ \"critical|warning\""], continue = true },
            # ...and EVERY alert (critical, warning, info) reaches people in Slack, with its resolution
            { receiver = "slack" },
          ]
        }
        receivers = [
          { name = "null" },
          { name = "incident-blog", webhook_configs = [{
            url           = local.incident_webhook
            send_resolved = false
            http_config   = { authorization = { type = "Bearer", credentials_file = "/etc/alertmanager/secrets/alertmanager-incident-blog/token" } }
          }] },
          { name = "slack", slack_configs = [{
            api_url_file  = "/etc/alertmanager/secrets/alertmanager-slack/webhook-url"
            send_resolved = true
            color         = "{{ if eq .Status \"firing\" }}{{ if eq .CommonLabels.severity \"critical\" }}danger{{ else }}warning{{ end }}{{ else }}good{{ end }}"
            title         = "[{{ .Status | toUpper }}] {{ .CommonLabels.alertname }} ({{ .CommonLabels.namespace }}, {{ .CommonLabels.severity }})"
            text          = "{{ range .Alerts }}{{ .Annotations.summary }}\n{{ end }}"
          }] },
        ]
      }
    }
    # Grafana: dashboards for the cluster, the apps and the Valkey cache. Admin password generated by
    # Terraform (never the chart's well-known default), kept in Secrets Manager as <prefix>/platform/grafana-admin-password.
    # passwordKey is only the NAME of the key in the Secret, not a password (reviewed Gitleaks false positive).
    grafana = {
      enabled = true
      admin   = { existingSecret = "grafana-admin", userKey = "admin-user", passwordKey = "admin-password" } # gitleaks:allow
      # Tempo (traces) and Loki (logs) from the OpenTelemetry pipeline (observability.tf), linked to each other
      additionalDataSources = local.grafana_otel_datasources
      # A dot on a latency graph (exemplar) opens the trace behind it
      sidecar = { datasources = { exemplarTraceIdDestinations = { datasourceUid = "tempo", traceIdLabelName = "trace_id" } } }
    }
  })]

  depends_on = [kubernetes_secret_v1.alertmanager_incident_token, kubernetes_secret_v1.alertmanager_slack, kubernetes_secret_v1.grafana_admin]
}

# EBS volumes for Tempo and Loki: gp3, encrypted, created in the zone of the pod that first uses them
resource "kubernetes_storage_class_v1" "gp3" {
  metadata {
    name        = "gp3"
    annotations = { "storageclass.kubernetes.io/is-default-class" = "true" }
  }
  storage_provisioner    = "ebs.csi.aws.com"
  reclaim_policy         = "Delete"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true
  parameters             = { type = "gp3", encrypted = "true" }
}
