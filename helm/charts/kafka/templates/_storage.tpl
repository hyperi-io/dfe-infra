{{/* The family half of storageModel. `tiered` moves closed segments to the bulk store. Empty for local. */}}
{{- define "dfe-kafka.storageFamily" -}}
{{- if ne .Values.kafka.storageModel "local" -}}
{{- first (splitList "-" .Values.kafka.storageModel) -}}
{{- end -}}
{{- end }}

{{/* The bulk half of storageModel: `object` for an object store. Empty for local. */}}
{{- define "dfe-kafka.storageBulk" -}}
{{- if ne .Values.kafka.storageModel "local" -}}
{{- last (splitList "-" .Values.kafka.storageModel) -}}
{{- end -}}
{{- end }}

{{/* Fails a tiered-storage request the deployment cannot honour. Strimzi drops unknown CR fields silently, so an operator too old for tieredStorage would tier nothing. */}}
{{- define "dfe-kafka.validateStorageModel" -}}
{{- $model := .Values.kafka.storageModel -}}
{{- if not (has $model (list "local" "tiered-object")) -}}
{{- fail (printf "kafka.storageModel must be local or tiered-object, got %q" $model) -}}
{{- end -}}
{{- if eq (include "dfe-kafka.storageBulk" .) "object" -}}
{{- if semverCompare "< 0.38.0" .Values.kafka.operatorVersion -}}
{{- fail (printf "kafka.storageModel=%s needs Strimzi >= 0.38.0 for spec.kafka.tieredStorage; kafka.operatorVersion is %s" $model .Values.kafka.operatorVersion) -}}
{{- end -}}
{{- if ne .Values.kafka.provider "strimzi" -}}
{{- fail (printf "kafka.storageModel=%s is a Strimzi Kafka CR field; kafka.provider is %s" $model .Values.kafka.provider) -}}
{{- end -}}
{{- if ne .Values.kafka.mode "cluster" -}}
{{- fail (printf "kafka.storageModel=%s needs kafka.mode=cluster (the operator reconciles it); kafka.mode is %s" $model .Values.kafka.mode) -}}
{{- end -}}
{{- if not .Values.kafka.tieredObject.className -}}
{{- fail "kafka.storageModel=tiered-object needs kafka.tieredObject.className -- Strimzi ships no RemoteStorageManager, so the broker image must carry one" -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/* The Kafka CR block that tiers closed segments to object storage. Empty unless the bulk store is object. */}}
{{- define "dfe-kafka.tieredStorage" -}}
{{- if eq (include "dfe-kafka.storageBulk" .) "object" -}}
tieredStorage:
  type: custom
  remoteStorageManager:
    className: {{ .Values.kafka.tieredObject.className | quote }}
    {{- with .Values.kafka.tieredObject.classPath }}
    classPath: {{ . | quote }}
    {{- end }}
    {{- with .Values.kafka.tieredObject.config }}
    config:
      {{- range $k, $v := . }}
      {{ $k }}: {{ $v | quote }}
      {{- end }}
    {{- end }}
{{- end -}}
{{- end }}

{{/* The object-store credential the RemoteStorageManager reads from the broker environment. */}}
{{- define "dfe-kafka.objectStoreEnv" -}}
- name: AWS_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ .Values.kafka.objectStore.secretName }}
      key: access_key_id
- name: AWS_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.kafka.objectStore.secretName }}
      key: secret_access_key
{{- end }}
