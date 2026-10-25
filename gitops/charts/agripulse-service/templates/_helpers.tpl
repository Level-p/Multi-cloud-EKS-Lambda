{{- define "svc.name" -}}
{{- required "service is required (gitops/services/<service>/values.yaml)" .Values.service -}}
{{- end }}

{{- define "svc.labels" -}}
app.kubernetes.io/part-of: agripulse
app.kubernetes.io/managed-by: {{ .Release.Service }}
agripulse.io/environment: {{ .Values.environment | default "unset" | quote }}
{{- end }}

{{- define "svc.podSecurity" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
seccompProfile: { type: RuntimeDefault }
{{- end }}

{{- define "svc.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities: { drop: ["ALL"] }
{{- end }}

{{/* The image the pipeline built, signed and committed: <registry>/<repository>:<tag>@<digest>. With the digest
     in Git, Kyverno's digest pinning changes nothing, so Argo CD sees no drift. In the cluster (requireBuiltImage,
     set by Terraform) the placeholder tag fails the render: Argo CD shows why and does not start a sync that
     could never succeed (and would block the first real commit while it retries). */}}
{{- define "svc.image" -}}
{{- if and .Values.requireBuiltImage (eq (toString .Values.image.tag) "initial") -}}
{{- fail (printf "No %s image has been built yet (image.tag is the placeholder 'initial'): run the %s pipeline; Argo CD deploys as soon as it commits the tag" .Values.service .Values.service) -}}
{{- end -}}
{{ .Values.image.registry }}/{{ .Values.image.repository }}:{{ .Values.image.tag }}{{ with .Values.image.digest }}@{{ . }}{{ end }}
{{- end }}

{{/* A Secrets Manager name: "platform/x" style names are absolute, the rest are under this environment's prefix */}}
{{- define "svc.secretName" -}}
{{- $s := index . 0 -}}{{- $root := index . 1 -}}
{{- if hasPrefix "/" $s.secret -}}{{ $root.Values.aws.secretRoot }}{{ $s.secret }}{{- else -}}{{ $root.Values.aws.secretPrefix }}/{{ $s.secret }}{{- end -}}
{{- end }}
