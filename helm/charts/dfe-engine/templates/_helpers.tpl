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

{{/*
dfe-engine.isDevPosture -- non-empty when `env` is one of the postures the
engine treats as dev.

The SAME list the engine's is_dev_posture uses (dfe-engine #300), which gates
the refuse-on-default-password check and gitops auto-merge.
*/}}
{{- define "dfe-engine.isDevPosture" -}}
{{- if has .Values.env (list "dev" "development" "local" "test" "ci") }}true{{ end }}
{{- end -}}

{{/*
An explicit hyperdx.baseUrl wins, for a fork outside the cluster. Otherwise it
is derived from the fork's API service, defaulting to this release's namespace
so a co-deployed fork needs no configuration at all.
*/}}
{{- define "dfe-engine.hyperdxBaseUrl" -}}
{{- with .Values.hyperdx.baseUrl -}}
{{ . }}
{{- else -}}
{{- $ns := .Values.hyperdx.namespace | default .Release.Namespace -}}
{{- printf "http://%s.%s.svc.cluster.local:%v" .Values.hyperdx.service $ns .Values.hyperdx.apiPort -}}
{{- end -}}
{{- end -}}
