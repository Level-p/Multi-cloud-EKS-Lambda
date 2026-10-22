{{- define "blog.labels" -}}
app.kubernetes.io/part-of: agripulse-incident-response
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "blog.podSecurity" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
seccompProfile: { type: RuntimeDefault }
{{- end }}

{{- define "blog.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities: { drop: ["ALL"] }
{{- end }}

{{/* Secrets Manager secrets mounted by the CSI driver (also synced into a Kubernetes Secret for env vars) */}}
{{- define "blog.secretsVolume" -}}
- name: secrets
  csi:
    driver: secrets-store.csi.k8s.io
    readOnly: true
    volumeAttributes: { secretProviderClass: incident-blog-secrets }
{{- end }}

{{/* PostgreSQL on RDS with IAM authentication: boto3 signs a token with the pod's role (EKS Pod Identity) */}}
{{- define "blog.dbEnv" -}}
- { name: DB_HOST, value: {{ .Values.db.host | quote }} }
- { name: DB_PORT, value: {{ .Values.db.port | toString | quote }} }
- { name: DB_NAME, value: {{ .Values.db.name | quote }} }
- { name: DB_USER, value: {{ .Values.db.user | quote }} }
- { name: AWS_REGION, value: {{ .Values.aws.region | quote }} }
{{- end }}

{{/* oauth2-proxy signs staff in with the Cognito staff user pool (OIDC authorization code flow) */}}
{{- define "blog.oauth2Args" -}}
- --provider=oidc
- --oidc-issuer-url={{ .Values.auth.issuerUrl }}
- --client-id={{ .Values.auth.clientId }}
- --scope=openid email profile
- --email-domain=*
{{- end }}
