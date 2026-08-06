{{/*
dfe-engine.clickhouseAuthEnv -- the account the engine and its sidecars connect
to ClickHouse as.

Defined once because all three workloads talk to the same server and a partial
rollout is the failure mode this chart has already seen with the JWT key: the
engine works, a sidecar does not, and it reads as two unrelated faults.

Emits nothing when clickhouse.user is unset, which leaves the previous
passwordless behaviour intact for a server that accepts it.

Usage:
  env:
    {{- include "dfe-engine.clickhouseAuthEnv" . | nindent 12 }}
*/}}
{{- define "dfe-engine.clickhouseAuthEnv" -}}
{{- with .Values.clickhouse.user }}
{{- /* DFE_CLICKHOUSE_USERNAME, not _USER: the settings field is `username`
       (ClickHouseSettings) and the loader takes the field name verbatim, so
       _USER is accepted silently and leaves the client on `default`. */}}
- name: DFE_CLICKHOUSE_USERNAME
  value: {{ . | quote }}
{{- end }}
{{- with .Values.clickhouse.passwordSecretName }}
- name: DFE_CLICKHOUSE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: {{ $.Values.clickhouse.passwordSecretKey | default "password" }}
{{- end }}
{{- end }}
