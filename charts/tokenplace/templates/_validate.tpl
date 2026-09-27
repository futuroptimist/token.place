{{- define "tokenplace.validateDeploymentContract" -}}
{{- $backend := .Values.stateBackend.type -}}
{{- $workers := int .Values.relay.workers -}}
{{- $ha := or (gt (int .Values.replicaCount) 1) (gt $workers 1) -}}
{{- if and (eq $backend "memory") $ha -}}
{{- fail "stateBackend.type=memory requires replicaCount=1 and relay.workers=1" -}}
{{- end -}}
{{- if and $ha (ne $backend "valkey") -}}
{{- fail "multiple replicas or workers require stateBackend.type=valkey" -}}
{{- end -}}
{{- if eq $backend "valkey" -}}
{{- if and (gt (int .Values.replicaCount) 1) (not .Values.podDisruptionBudget.enabled) -}}{{ fail "multiple replicas require podDisruptionBudget.enabled=true" }}{{- end -}}
{{- if and (gt (int .Values.replicaCount) 1) (not .Values.podAntiAffinity.enabled) (not .Values.topologySpreadConstraints) -}}{{ fail "multiple replicas require podAntiAffinity or topologySpreadConstraints" }}{{- end -}}
{{- if ne .Values.strategy.type "RollingUpdate" -}}{{ fail "stateBackend.type=valkey requires strategy.type=RollingUpdate" }}{{- end -}}
{{- if not .Values.preStop.enabled -}}{{ fail "stateBackend.type=valkey requires preStop.enabled=true" }}{{- end -}}
{{- if lt (int .Values.terminationGracePeriodSeconds) (int .Values.preStop.sleepSeconds) -}}{{ fail "terminationGracePeriodSeconds must be at least preStop.sleepSeconds" }}{{- end -}}
{{- $_ := required "stateBackend.valkey.environment is required" .Values.stateBackend.valkey.environment -}}
{{- $_ := required "stateBackend.valkey.cluster is required" .Values.stateBackend.valkey.cluster -}}
{{- $_ := required "stateBackend.valkey.acknowledgementKey.existingSecret is required" .Values.stateBackend.valkey.acknowledgementKey.existingSecret -}}
{{- $_ := required "sharedRateLimit.existingSecret is required for Valkey" .Values.sharedRateLimit.existingSecret -}}
{{- if eq .Values.stateBackend.valkey.discovery "direct" -}}
{{- $_ := required "stateBackend.valkey.direct.host is required for direct discovery" .Values.stateBackend.valkey.direct.host -}}
{{- else if eq .Values.stateBackend.valkey.discovery "sentinel" -}}
{{- $_ := required "stateBackend.valkey.sentinel.service is required for Sentinel discovery" .Values.stateBackend.valkey.sentinel.service -}}
{{- if not .Values.stateBackend.valkey.sentinel.endpoints -}}{{ fail "stateBackend.valkey.sentinel.endpoints is required for Sentinel discovery" }}{{- end -}}
{{- else -}}{{ fail "stateBackend.valkey.discovery must be direct or sentinel" }}{{- end -}}
{{- if and .Values.stateBackend.valkey.tls.enabled (not .Values.stateBackend.valkey.tls.existingSecret) -}}{{ fail "stateBackend.valkey.tls.existingSecret is required when TLS is enabled" }}{{- end -}}
{{- if ne (not (empty .Values.stateBackend.valkey.tls.clientCertKey)) (not (empty .Values.stateBackend.valkey.tls.clientKeyKey)) -}}{{ fail "stateBackend.valkey.tls.clientCertKey and clientKeyKey must either both be set or both be empty" }}{{- end -}}
{{- range $source := list .Values.env .Values.extraEnv -}}
{{- range $key, $entry := $source -}}
{{- $name := $key -}}{{- if and (kindIs "map" $entry) (hasKey $entry "name") -}}{{- $name = get $entry "name" -}}{{- end -}}
{{- if or (eq (printf "%v" $name) "TOKENPLACE_RELAY_STATE_BACKEND") (hasPrefix "TOKENPLACE_RELAY_VALKEY_" (printf "%v" $name)) (eq (printf "%v" $name) "RELAY_WORKERS") (eq (printf "%v" $name) "TOKENPLACE_RATE_LIMIT_STORAGE_URI") (eq (printf "%v" $name) "TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES") -}}
{{- fail (printf "environment override %s is chart-managed when Valkey is selected" $name) -}}
{{- end -}}
{{- end -}}{{- end -}}
{{- end -}}
{{- end -}}
