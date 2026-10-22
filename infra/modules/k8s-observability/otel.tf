# ---------------------------------------------------------------------------
# Observability with OpenTelemetry and Grafana, in the "monitoring" namespace next to kube-prometheus-stack:
#
#   web, incident blog (OpenTelemetry SDK) --OTLP--> OpenTelemetry Collector --> Tempo  (traces)
#                                                                           \--> Loki   (logs, with trace ids)
#   Tempo --span metrics + service graph (remote write, with exemplars)--> Prometheus
#   Grafana: Prometheus, Tempo and Loki linked both ways (metric -> trace -> logs -> trace),
#            and the dashboard "AgriPulse services (OpenTelemetry)"
#
# Metrics stay where they were (Prometheus scrapes /metrics); Tempo adds rate/errors/duration per service
# and operation derived from the traces. Nothing is exposed outside the cluster: Grafana is reached with
# kubectl port-forward, and network policies (enforced by the VPC CNI) let only the apps, Grafana and
# Prometheus talk to these.
# ---------------------------------------------------------------------------

locals {
  monitoring_ns  = "monitoring" # kubernetes_namespace_v1.monitoring; the releases below wait for helm_release.monitoring
  prometheus_url = "http://kube-prometheus-stack-prometheus.${local.monitoring_ns}.svc:9090"

  # Grafana data sources (helm_release.monitoring in platform.tf). Prometheus (uid "prometheus") comes with the stack.
  grafana_otel_datasources = [
    {
      name   = "Tempo"
      type   = "tempo"
      uid    = "tempo"
      access = "proxy"
      url    = "http://tempo.${local.monitoring_ns}.svc:3200"
      jsonData = {
        tracesToLogsV2  = { datasourceUid = "loki", filterByTraceID = true, spanStartTimeShift = "-5m", spanEndTimeShift = "5m" }
        tracesToMetrics = { datasourceUid = "prometheus" }
        serviceMap      = { datasourceUid = "prometheus" }
        nodeGraph       = { enabled = true }
        lokiSearch      = { datasourceUid = "loki" }
      }
    },
    {
      name   = "Loki"
      type   = "loki"
      uid    = "loki"
      access = "proxy"
      url    = "http://loki.${local.monitoring_ns}.svc:3100"
      jsonData = {
        # OTLP logs keep the trace id as structured metadata "trace_id": one click opens the trace
        derivedFields = [{ name = "TraceID", matcherType = "label", matcherRegex = "trace_id", datasourceUid = "tempo", url = "$${__value.raw}", urlDisplayLabel = "View trace" }]
      }
    },
  ]
}

# ---------------- chart values ----------------
locals {
  tempo_values = {
    tempo = {
      reportingEnabled = false
      retention        = "72h"
      # The collector sends OTLP. (The chart's Service template needs its default Jaeger receiver; the network
      # policy below only lets OTLP from the collector in.)
      receivers = { otlp = { protocols = { grpc = { endpoint = "0.0.0.0:4317" }, http = { endpoint = "0.0.0.0:4318" } } } }
      # Rate, errors and duration per service and operation, and the service graph, written to Prometheus
      # with exemplars (trace ids), so a latency spike links straight to a slow trace
      metricsGenerator = {
        enabled = true
        storage = {
          path         = "/var/tempo/metrics"
          remote_write = [{ url = "${local.prometheus_url}/api/v1/write", send_exemplars = true }]
        }
      }
      overrides = { defaults = { metrics_generator = { processors = ["service-graphs", "span-metrics"] } } }
      resources = { requests = { cpu = "100m", memory = "256Mi" }, limits = { memory = "1Gi" } }
    }
    persistence    = { enabled = true, size = "10Gi" }
    serviceMonitor = { enabled = true }
  }

  loki_values = {
    deploymentMode = "Monolithic" # one pod with a disk: right for the volume of two apps
    loki = {
      auth_enabled = false # one tenant; access is limited by the network policy below
      commonConfig = { replication_factor = 1 }
      storage      = { type = "filesystem" }
      schemaConfig = {
        configs = [{ from = "2026-01-01", store = "tsdb", object_store = "filesystem", schema = "v13", index = { prefix = "index_", period = "24h" } }]
      }
      limits_config = { retention_period = "168h", allow_structured_metadata = true } # OTLP attributes, trace ids
      compactor     = { retention_enabled = true, delete_request_store = "filesystem" }
    }
    singleBinary = {
      replicas    = 1
      persistence = { size = "20Gi" }
      resources   = { requests = { cpu = "100m", memory = "256Mi" }, limits = { memory = "1Gi" } }
    }
    read         = { replicas = 0 } # the chart's simple-scalable parts are off in monolithic mode
    write        = { replicas = 0 }
    backend      = { replicas = 0 }
    gateway      = { enabled = false } # Grafana and the collector reach Loki directly inside the cluster
    chunksCache  = { enabled = false }
    resultsCache = { enabled = false }
    lokiCanary   = { enabled = false }
    test         = { enabled = false }
  }

  otel_collector_values = {
    mode             = "deployment"
    replicaCount     = 2 # stateless: either replica takes any request
    fullnameOverride = "otel-collector"
    image            = { repository = "otel/opentelemetry-collector-k8s" }
    presets          = { kubernetesAttributes = { enabled = true } } # adds pod, namespace, deployment to every span and log
    ports = {
      jaeger-compact = { enabled = false }
      jaeger-thrift  = { enabled = false }
      jaeger-grpc    = { enabled = false }
      zipkin         = { enabled = false }
      metrics        = { enabled = true } # the collector's own metrics, for the ServiceMonitor
    }
    serviceMonitor = { enabled = true }
    resources      = { requests = { cpu = "50m", memory = "128Mi" }, limits = { memory = "512Mi" } }
    config = {
      receivers = { jaeger = null, zipkin = null, prometheus = null } # OTLP only
      exporters = {
        debug = null
        # Collector 0.16x component names (otlp_grpc, otlp_http, k8s_attributes; the chart would rewrite the old ones)
        "otlp_grpc/tempo" = { endpoint = "tempo.${local.monitoring_ns}.svc:4317", tls = { insecure = true } } # plain gRPC inside the cluster
        "otlp_http/loki"  = { endpoint = "http://loki.${local.monitoring_ns}.svc:3100/otlp" }
      }
      service = {
        pipelines = {
          traces  = { receivers = ["otlp"], processors = ["memory_limiter", "k8s_attributes", "batch"], exporters = ["otlp_grpc/tempo"] }
          logs    = { receivers = ["otlp"], processors = ["memory_limiter", "k8s_attributes", "batch"], exporters = ["otlp_http/loki"] }
          metrics = null # the apps' metrics are scraped by Prometheus
        }
      }
    }
  }
}

# ---------------- Tempo: traces ----------------
resource "helm_release" "tempo" {
  name       = "tempo"
  repository = "https://grafana-community.github.io/helm-charts" # the chart moved here from grafana/helm-charts
  chart      = "tempo"
  version    = "3.2.0" # Tempo 3.1.0
  namespace  = local.monitoring_ns

  values = [yamlencode(local.tempo_values)]

  depends_on = [helm_release.monitoring, kubernetes_storage_class_v1.gp3] # its disk is a gp3 EBS volume
}

# ---------------- Loki: logs ----------------
resource "helm_release" "loki" {
  name       = "loki"
  repository = "https://grafana-community.github.io/helm-charts"
  chart      = "loki"
  version    = "18.16.0" # Loki 3.7.8
  namespace  = local.monitoring_ns

  values = [yamlencode(local.loki_values)]

  depends_on = [helm_release.monitoring, kubernetes_storage_class_v1.gp3] # its disk is a gp3 EBS volume
}

# ---------------- OpenTelemetry Collector ----------------
resource "helm_release" "otel_collector" {
  name       = "otel-collector"
  repository = "https://open-telemetry.github.io/opentelemetry-helm-charts"
  chart      = "opentelemetry-collector"
  version    = "0.176.0" # collector 0.162.0
  namespace  = local.monitoring_ns

  values = [yamlencode(local.otel_collector_values)]

  depends_on = [helm_release.tempo, helm_release.loki]
}

# ---------------- who may talk to whom ----------------
# Tempo and Loki have no authentication of their own: only the collector writes, only Grafana reads,
# only Prometheus scrapes. The collector accepts OTLP from the two app namespaces only.
# Each also accepts its own pods on every port (memberlist and gRPC through its own Service).
locals {
  observability_policies = {
    tempo = {
      pod = { "app.kubernetes.io/name" = "tempo" }
      from = [{ pod = { "app.kubernetes.io/name" = "opentelemetry-collector" }, ports = [4317] },
        { pod = { "app.kubernetes.io/name" = "grafana" }, ports = [3200] }, { pod = { "app.kubernetes.io/name" = "prometheus" }, ports = [3200] },
      { pod = { "app.kubernetes.io/name" = "tempo" }, ports = [] }]
    }
    loki = {
      pod = { "app.kubernetes.io/name" = "loki" }
      from = [{ pod = { "app.kubernetes.io/name" = "opentelemetry-collector" }, ports = [3100] },
        { pod = { "app.kubernetes.io/name" = "grafana" }, ports = [3100] }, { pod = { "app.kubernetes.io/name" = "prometheus" }, ports = [3100] },
      { pod = { "app.kubernetes.io/name" = "loki" }, ports = [] }]
    }
    otel-collector = {
      pod = { "app.kubernetes.io/name" = "opentelemetry-collector" }
      from = concat([for ns in var.app_namespaces : { namespace = ns, ports = [4317, 4318] }],
      [{ pod = { "app.kubernetes.io/name" = "prometheus" }, ports = [8888] }])
    }
  }
}

resource "kubernetes_network_policy_v1" "observability" {
  for_each = local.observability_policies
  metadata {
    name      = "${each.key}-ingress"
    namespace = local.monitoring_ns
  }
  spec {
    pod_selector { match_labels = each.value.pod }
    policy_types = ["Ingress"]
    dynamic "ingress" {
      for_each = each.value.from
      content {
        from {
          dynamic "pod_selector" {
            for_each = lookup(ingress.value, "pod", null) == null ? [] : [ingress.value.pod]
            content { match_labels = pod_selector.value }
          }
          dynamic "namespace_selector" {
            for_each = lookup(ingress.value, "namespace", null) == null ? [] : [ingress.value.namespace]
            content { match_labels = { "kubernetes.io/metadata.name" = namespace_selector.value } }
          }
        }
        dynamic "ports" {
          for_each = ingress.value.ports
          content {
            port     = ports.value
            protocol = "TCP"
          }
        }
      }
    }
  }
  depends_on = [helm_release.otel_collector]
}

# ---------------- Grafana dashboard ----------------
# The Grafana sidecar loads every ConfigMap labelled grafana_dashboard=1
resource "kubernetes_config_map_v1" "grafana_dashboard_otel" {
  metadata {
    name      = "grafana-dashboard-agripulse-otel"
    namespace = local.monitoring_ns
    labels    = { grafana_dashboard = "1" }
  }
  data       = { "agripulse-opentelemetry.json" = file("${path.module}/dashboards/agripulse-opentelemetry.json") }
  depends_on = [helm_release.monitoring]
}
