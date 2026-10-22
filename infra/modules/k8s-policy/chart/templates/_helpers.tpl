{{/* Pods created or updated in the app namespaces */}}
{{- define "policies.match" -}}
resourceRules:
  - apiGroups: [""]
    apiVersions: ["v1"]
    operations: ["CREATE", "UPDATE"]
    resources: ["pods"]
namespaceSelector:
  matchExpressions:
    - key: kubernetes.io/metadata.name
      operator: In
      values: {{ toJson .Values.namespaces }}
{{- end }}
