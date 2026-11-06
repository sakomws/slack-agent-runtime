{{- define "slack-agent-runtime.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "slack-agent-runtime.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "slack-agent-runtime.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "slack-agent-runtime.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "slack-agent-runtime.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "slack-agent-runtime.selectorLabels" -}}
app.kubernetes.io/name: {{ include "slack-agent-runtime.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "slack-agent-runtime.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "slack-agent-runtime.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
