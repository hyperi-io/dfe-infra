{{/*
dfe-kafka.validateStorageModel -- refuse a tiered-storage request the deployment
cannot actually honour, at render time.

Strimzi drops unknown CR fields silently, so an operator too old for
spec.kafka.tieredStorage would deploy a broker that looks configured and tiers
nothing. Called from validate.yaml so it fires in every mode.
*/}}
{{- define "dfe-kafka.validateStorageModel" -}}
{{- $model := .Values.kafka.storageModel -}}
{{- if not (has $model (list "local" "tiered")) -}}
{{- fail (printf "kafka.storageModel must be local or tiered, got %q" $model) -}}
{{- end -}}
{{- if eq $model "tiered" -}}
{{- if semverCompare "< 0.38.0" .Values.kafka.operatorVersion -}}
{{- fail (printf "kafka.storageModel=tiered needs Strimzi >= 0.38.0 for spec.kafka.tieredStorage; kafka.operatorVersion is %s" .Values.kafka.operatorVersion) -}}
{{- end -}}
{{- if ne .Values.kafka.provider "strimzi" -}}
{{- fail (printf "kafka.storageModel=tiered is a Strimzi Kafka CR field; kafka.provider is %s" .Values.kafka.provider) -}}
{{- end -}}
{{- if ne .Values.kafka.mode "cluster" -}}
{{- fail (printf "kafka.storageModel=tiered needs kafka.mode=cluster (the operator reconciles it); kafka.mode is %s" .Values.kafka.mode) -}}
{{- end -}}
{{- if not .Values.kafka.tiered.className -}}
{{- fail "kafka.storageModel=tiered needs kafka.tiered.className -- Strimzi ships no RemoteStorageManager, so the broker image must carry one" -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
dfe-kafka.tieredStorage -- the Kafka CR block that moves closed segments to
object storage. Emits nothing unless storageModel is tiered.
*/}}
{{- define "dfe-kafka.tieredStorage" -}}
{{- if eq .Values.kafka.storageModel "tiered" -}}
tieredStorage:
  type: custom
  remoteStorageManager:
    className: {{ .Values.kafka.tiered.className | quote }}
    {{- with .Values.kafka.tiered.classPath }}
    classPath: {{ . | quote }}
    {{- end }}
    {{- with .Values.kafka.tiered.config }}
    config:
      {{- range $k, $v := . }}
      {{ $k }}: {{ $v | quote }}
      {{- end }}
    {{- end }}
{{- end -}}
{{- end }}

{{/*
dfe-kafka.tieredEnv -- the object-store credential the RemoteStorageManager
reads from the broker environment.
*/}}
{{- define "dfe-kafka.tieredEnv" -}}
- name: AWS_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ .Values.kafka.tiered.secretName }}
      key: access_key_id
- name: AWS_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.kafka.tiered.secretName }}
      key: secret_access_key
{{- end }}
