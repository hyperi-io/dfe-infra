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
dfe-engine.e2eServer -- non-empty when this deployment runs in the e2e posture.

The posture mounts the engine's unauthenticated /api/e2e routes, whose seed
scripts wipe every account, so anything but a dev posture fails the render. An
appset parameter can arrive as a bool or a string, so the value is compared as
text: only true turns it on, and anything but false or empty is refused rather
than read as truthy.
*/}}
{{- define "dfe-engine.e2eServer" -}}
{{- $value := toString .Values.e2eServer -}}
{{- if eq $value "true" -}}
{{- if not (include "dfe-engine.isDevPosture" .) -}}
{{- fail (printf "e2eServer is on and env is %q: the e2e seed routes wipe every account, so they render only on a dev posture (dev, development, local, test, ci)" (toString .Values.env)) -}}
{{- end -}}
true
{{- else if not (has $value (list "false" "" "<nil>")) -}}
{{- fail (printf "e2eServer must be true or false, got %q" $value) -}}
{{- end -}}
{{- end -}}

{{/*
dfe-engine.contentRoot -- where one content entry's files land, from its role.

The role is the destination and the kind is the vehicle, and the two are
independent: a copy out of an image, a download or an app's own emit can each
carry any of the three sorts of file.
*/}}
{{- define "dfe-engine.contentRoot" -}}
{{- $mount := .ctx.Values.content.mountPath -}}
{{- $role := .entry.role | default "library" -}}
{{- if eq $role "catalogue" -}}
{{ printf "%s/catalogue" $mount }}
{{- else if eq $role "contract" -}}
{{- /* One directory per app under this root, so the engine reads a service's
       contract by name without being told which image wrote it. */ -}}
{{ printf "%s/contract" $mount }}
{{- else -}}
{{ printf "%s/library" $mount }}
{{- end -}}
{{- end -}}

{{/*
dfe-engine.catalogueFile -- the mounted source catalogue, or empty when this
deployment materialises none.

Empty is the answer for most deployments, and the engine offers no catalogue
sources rather than failing: DFE_SOURCE_CATALOGUE_FILE naming a file nothing
wrote is a deployment that meant to mount one and did not.
*/}}
{{- define "dfe-engine.catalogueFile" -}}
{{- $found := "" -}}
{{- range .Values.content.entries -}}
{{- if eq (.role | default "library") "catalogue" -}}
{{- $found = "yes" -}}
{{- end -}}
{{- end -}}
{{- if $found -}}
{{ printf "%s/catalogue/%s" .Values.content.mountPath .Values.content.catalogueFile }}
{{- end -}}
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
