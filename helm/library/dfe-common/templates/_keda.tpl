{{/*
dfe-common.scaledobject — renders a KEDA ScaledObject for a dfe-* app from its
.Values.keda block. KEDA is folded INTO each app chart (not a side-car chart) so
the engine's per-instance overlay drives scaling directly: the overlay sets
.Values.keda and this renders the matching ScaledObject.

scaleTargetRef is the app's own Deployment (dfe-common.fullname). Triggers are
passed through verbatim from .Values.keda.triggers (any KEDA scaler type), so the
engine's HelmKedaConfig output maps 1:1.

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
    {{- toYaml .Values.keda.triggers | nindent 4 }}
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
