{{/*
dfe-common.scaledobject — renders a KEDA ScaledObject for a dfe-* app from its
.Values.keda block. KEDA is folded INTO each app chart (not a side-car chart) so
the engine's per-instance overlay drives scaling directly: the overlay sets
.Values.keda and this renders the matching ScaledObject.

scaleTargetRef is the app's own Deployment (dfe-common.fullname). The DEFAULT
trigger is CPU utilisation (native KEDA cpu scaler - zero code, zero upstream risk,
the safe fleet baseline; metrics-server is installed and the pods declare CPU
requests). scalo ScalingPressure is OPT-IN per app (.Values.keda.pressure.enabled)
and routes through the fail-safe dfe-keda-shim so a metric outage FREEZES scaling
rather than running replicas up. An app may still set .Values.keda.triggers to
override VERBATIM (any KEDA scaler type, e.g. Kafka lag), so the engine's
HelmKedaConfig output still maps 1:1 when it emits explicit triggers.

NOTE: minReplicaCount defaults to 1 -- scale-to-zero (min/idle 0) is 2.2 backlog
(kafka-pipeline-only idle shutdown; the 2.2 pipeline is efficient enough that one
always-on container is acceptable). Set keda.minReplicaCount/idleReplicaCount to 0
via the overlay to opt a pipeline app into scale-to-zero later.

Usage (templates/scaledobject.yaml):
  {{- include "dfe-common.scaledobject" . }}
*/}}
{{- define "dfe-common.scaledobject" -}}
{{- if and .Values.keda .Values.keda.enabled -}}
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: {{ include "dfe-common.fullname" . }}-scaler
  labels:
    {{- include "dfe-common.labels" . | nindent 4 }}
spec:
  scaleTargetRef:
    name: {{ include "dfe-common.fullname" . }}
  minReplicaCount: {{ .Values.keda.minReplicaCount | default 1 }}
  maxReplicaCount: {{ .Values.keda.maxReplicaCount | default 10 }}
  {{- if hasKey .Values.keda "idleReplicaCount" }}
  idleReplicaCount: {{ .Values.keda.idleReplicaCount }}
  {{- end }}
  cooldownPeriod: {{ .Values.keda.cooldownPeriod | default 300 }}
  pollingInterval: {{ .Values.keda.pollingInterval | default 30 }}
  triggers:
    {{- if .Values.keda.triggers }}
    {{- /* Verbatim override: any KEDA scaler, passed straight through (e.g. Kafka lag). */}}
    {{- toYaml .Values.keda.triggers | nindent 4 }}
    {{- else }}
    {{- /* DEFAULT: CPU utilisation - native KEDA cpu scaler, the safe fleet baseline. */}}
    {{- $cpu := .Values.keda.cpu | default dict }}
    - type: cpu
      metricType: Utilization
      metadata:
        value: {{ $cpu.targetUtilization | default 70 | quote }}
    {{- /* OPT-IN: scalo ScalingPressure (0-100) via the fail-safe dfe-keda-shim - a
           metric outage FREEZES scaling, never runs replicas up. metricType Value:
           the intensive 0-100 gauge, NOT AverageValue (which would mis-scale). */}}
    {{- $p := .Values.keda.pressure | default dict }}
    {{- if $p.enabled }}
    - type: metrics-api
      metricType: Value
      metadata:
        targetValue: {{ $p.targetValue | default 70 | quote }}
        url: {{ printf "http://%s/keda/pressure?service=%s" ($p.shimAddress | default "dfe-keda-shim.dfe.svc.cluster.local:8080") ($p.service | default (include "dfe-common.fullname" .)) | quote }}
        valueLocation: "value"
    {{- end }}
    {{- end }}
{{- end -}}
{{- end -}}

{{/*
dfe-common.triggerauthentication — optional KEDA TriggerAuthentication, rendered
when .Values.keda.triggerAuthentication.secretTargetRef is set (e.g. Kafka SASL
for lag triggers). Reference it from a trigger via authenticationRef:
{name: <fullname>-trigger-auth}.

Usage (templates/scaledobject.yaml, before the ScaledObject + a --- separator):
  {{- include "dfe-common.triggerauthentication" . }}
*/}}
{{- define "dfe-common.triggerauthentication" -}}
{{- if and .Values.keda .Values.keda.enabled .Values.keda.triggerAuthentication -}}
apiVersion: keda.sh/v1alpha1
kind: TriggerAuthentication
metadata:
  name: {{ include "dfe-common.fullname" . }}-trigger-auth
  labels:
    {{- include "dfe-common.labels" . | nindent 4 }}
spec:
  secretTargetRef:
    {{- toYaml .Values.keda.triggerAuthentication.secretTargetRef | nindent 4 }}
{{- end -}}
{{- end -}}
