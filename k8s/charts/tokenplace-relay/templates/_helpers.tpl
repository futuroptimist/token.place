{{- define "tokenplace-relay.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "tokenplace-relay.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "tokenplace-relay.labels" -}}
app.kubernetes.io/name: {{ include "tokenplace-relay.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: tokenplace
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end -}}

{{- define "tokenplace-relay.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tokenplace-relay.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "tokenplace-relay.image" -}}
{{- $repo := .Values.image.repository -}}
{{- if .Values.image.digest -}}
{{ printf "%s@%s" $repo .Values.image.digest }}
{{- else if .Values.image.tag -}}
{{ printf "%s:%s" $repo .Values.image.tag }}
{{- else -}}
{{ printf "%s:%s" $repo .Chart.AppVersion }}
{{- end -}}
{{- end -}}
