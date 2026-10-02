{{- define "tokenplace.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "tokenplace.validateHA" -}}
{{- $backend := .Values.stateBackend.type -}}
{{- $ha := or (gt (int .Values.replicaCount) 1) (gt (int .Values.relay.workers) 1) -}}
{{- if and $ha (ne $backend "valkey") -}}{{ fail "multiple replicas/workers require stateBackend.type=valkey" }}{{- end -}}
{{- if and (eq $backend "valkey") .Values.relay.legacyRoutes.enabled -}}{{ fail "Valkey HA mode requires legacy relay routes to be disabled" }}{{- end -}}
{{- if eq $backend "valkey" -}}
  {{- if not .Values.stateBackend.valkey.environment -}}{{ fail "stateBackend.valkey.environment is required" }}{{- end -}}
  {{- if not .Values.stateBackend.valkey.cluster -}}{{ fail "stateBackend.valkey.cluster is required" }}{{- end -}}
  {{- if not .Values.stateBackend.valkey.acknowledgementKey.existingSecret -}}{{ fail "stateBackend.valkey.acknowledgementKey.existingSecret is required" }}{{- end -}}
  {{- if not .Values.rateLimit.storage.existingSecret -}}{{ fail "rateLimit.storage.existingSecret is required with Valkey" }}{{- end -}}
  {{- if not .Values.stateBackend.valkey.auth.existingSecret -}}{{ fail "stateBackend.valkey.auth.existingSecret is required with Valkey" }}{{- end -}}
  {{- if eq .Values.stateBackend.valkey.discovery "direct" -}}
    {{- if not .Values.stateBackend.valkey.direct.host -}}{{ fail "stateBackend.valkey.direct.host is required for direct discovery" }}{{- end -}}
  {{- else if eq .Values.stateBackend.valkey.discovery "sentinel" -}}
    {{- if not .Values.stateBackend.valkey.sentinel.endpointsJson -}}{{ fail "stateBackend.valkey.sentinel.endpointsJson is required for Sentinel discovery" }}{{- end -}}
    {{- if not .Values.stateBackend.valkey.sentinel.service -}}{{ fail "stateBackend.valkey.sentinel.service is required for Sentinel discovery" }}{{- end -}}
    {{- if not .Values.stateBackend.valkey.sentinel.auth.existingSecret -}}{{ fail "stateBackend.valkey.sentinel.auth.existingSecret is required for Sentinel discovery" }}{{- end -}}
  {{- else -}}{{ fail "stateBackend.valkey.discovery must be direct or sentinel" }}{{- end -}}
  {{- if and .Values.stateBackend.valkey.tls.enabled (not .Values.stateBackend.valkey.tls.existingSecret) -}}{{ fail "stateBackend.valkey.tls.existingSecret is required when TLS is enabled" }}{{- end -}}
{{- end -}}
{{- if and $ha (ne .Values.strategy.type "RollingUpdate") -}}{{ fail "multiple replicas/workers require strategy.type=RollingUpdate" }}{{- end -}}
{{- if and $ha (not .Values.podDisruptionBudget.enabled) -}}{{ fail "multiple replicas/workers require podDisruptionBudget.enabled=true" }}{{- end -}}
{{- if and .Values.podDisruptionBudget.enabled (ge (int .Values.podDisruptionBudget.maxUnavailable) (int .Values.replicaCount)) -}}{{ fail "podDisruptionBudget.maxUnavailable must be less than replicaCount" }}{{- end -}}
{{- end -}}

{{- define "tokenplace.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := include "tokenplace.name" . -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "tokenplace.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "tokenplace.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "tokenplace.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tokenplace.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "tokenplace.image" -}}
{{- $repo := .Values.image.repository -}}
{{- if .Values.image.digest -}}
{{ printf "%s@%s" $repo .Values.image.digest }}
{{- else if .Values.image.tag -}}
{{ printf "%s:%s" $repo .Values.image.tag }}
{{- else -}}
{{ printf "%s:%s" $repo .Chart.AppVersion }}
{{- end -}}
{{- end -}}
