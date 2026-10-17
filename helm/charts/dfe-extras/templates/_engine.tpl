{{/*
dfe-engine.clickhouseAuthEnv -- the admin account the engine connects to
ClickHouse as, to create every other user. No other workload takes it: the hunt
runner and the keda shim connect as their own users (serviceClickhouseAuthEnv).

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
dfe-engine.serviceClickhouseAuthEnv -- an app's own ClickHouse user, in place of
clickhouseAuthEnv. Takes the identity (dict "user" "passwordSecretName"
"passwordSecretKey"). The password is the Secret the engine adopts for that user.

Usage:
  env:
    {{- include "dfe-engine.serviceClickhouseAuthEnv" .Values.huntRunner.clickhouse | nindent 12 }}
*/}}
{{- define "dfe-engine.serviceClickhouseAuthEnv" -}}
- name: DFE_CLICKHOUSE_USERNAME
  value: {{ .user | quote }}
- name: DFE_CLICKHOUSE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .passwordSecretName }}
      key: {{ .passwordSecretKey | default "password" }}
{{- end }}

{{/*
dfe-engine.huntRunnerClickhouseAuthEnv -- the hunt runner's own ClickHouse user.
*/}}
{{- define "dfe-engine.huntRunnerClickhouseAuthEnv" -}}
{{- include "dfe-engine.serviceClickhouseAuthEnv" .Values.huntRunner.clickhouse }}
{{- end }}

{{/*
dfe-engine.clickhousePasswordSecret -- the Secret one app's ClickHouse password
lives in, which both the engine and that app's pod read. Takes (dict "ctx" .
"name" <Secret> "key" <data key> "password" <pinned value, or empty>).

GENERATED IN-CLUSTER by ESO's Password generator with refreshPolicy CreatedOnce,
for the reasons engine-jwt-secret.yaml gives: Argo renders with `helm template`,
so a template-minted value would change on every sync, and Reloader would roll
both pods onto a password the other had not been handed yet. A pinned password
renders a plain Secret instead.
*/}}
{{- define "dfe-engine.clickhousePasswordSecret" -}}
{{- if .password }}
apiVersion: v1
kind: Secret
metadata:
  name: {{ .name }}
  labels:
    {{- include "dfe-common.labels" .ctx | nindent 4 }}
  annotations:
    # keep: a reinstall that minted a different password would leave the engine
    # and the app on two values until both rolled.
    helm.sh/resource-policy: keep
type: Opaque
stringData:
  {{ .key }}: {{ .password | quote }}
{{- else }}
apiVersion: generators.external-secrets.io/v1alpha1
kind: Password
metadata:
  name: {{ .name }}-gen
  labels:
    {{- include "dfe-common.labels" .ctx | nindent 4 }}
spec:
  length: 24
  digits: 4
  symbols: 0          # alphanumeric -- the value is a ClickHouse password passed through env
  noUpper: false
  allowRepeat: true
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ .name }}
  labels:
    {{- include "dfe-common.labels" .ctx | nindent 4 }}
  annotations:
    # The Secret carries ESO's ownerReference, so deleting this ExternalSecret by
    # hand garbage-collects the password with it.
    helm.sh/resource-policy: keep
spec:
  refreshPolicy: CreatedOnce
  refreshInterval: "0"
  target:
    name: {{ .name }}
    creationPolicy: Owner
    template:
      engineVersion: v2
      data:
        {{ .key }}: "{{ `{{ .password }}` }}"
  dataFrom:
    - sourceRef:
        generatorRef:
          apiVersion: generators.external-secrets.io/v1alpha1
          kind: Password
          name: {{ .name }}-gen
{{- end }}
{{- end }}

{{/*
dfe-engine.isDevPosture -- non-empty when `env` is one of the postures the
engine treats as dev.

The SAME list the engine's is_dev_posture uses (dfe-engine #300), which gates
the refuse-on-default-password check and gitops auto-merge, compared the way it
compares: trimmed and lower-cased.
*/}}
{{- define "dfe-engine.isDevPosture" -}}
{{- if has (lower (trim (toString .Values.env))) (list "dev" "development" "local" "test" "ci") }}true{{ end }}
{{- end -}}

{{/*
dfe-engine.docsEnabled -- api.docsEnabled as "true" or "false", or empty when
unset, which leaves the engine to serve /docs and /redoc on a dev posture only.

Anything else fails the render: the gateway and the links page read the same key
(argocd/values/common.yaml), and a value the engine reads one way and they read
another publishes a page that is not served, or serves one nobody routes.
*/}}
{{- define "dfe-engine.docsEnabled" -}}
{{- $v := toString .Values.api.docsEnabled -}}
{{- if has $v (list "true" "false") -}}
{{ $v }}
{{- else if not (has $v (list "" "<nil>")) -}}
{{- fail (printf "api.docsEnabled is %q -- it takes true, false or empty (served on a dev posture only), because the engine, the gateway and the links page each read it and must agree" $v) -}}
{{- end -}}
{{- end -}}

{{/*
dfe-engine.optionalBool -- an optional engine switch as "true" or "false", or
empty when unset. Takes (dict "key" <values path> "value" <value>).

The engine reads any word but true, 1 and yes as false, so a typo would turn a
protection off without a word; it fails the render instead.
*/}}
{{- define "dfe-engine.optionalBool" -}}
{{- $v := toString .value -}}
{{- if has $v (list "true" "false") -}}
{{ $v }}
{{- else if not (has $v (list "" "<nil>")) -}}
{{- fail (printf "%s is %q -- it takes true, false or empty for the engine's default" .key $v) -}}
{{- end -}}
{{- end -}}

{{/*
dfe-engine.optionalCount -- an optional whole-number engine setting, or empty
when unset. Takes (dict "key" <values path> "value" <value> "min" <floor>).

A number from a values file arrives as a float64, which renders 1e+06 bare and
the engine cannot parse; one under the engine's own floor stops the engine at
startup, so both fail the render by name instead.
*/}}
{{- define "dfe-engine.optionalCount" -}}
{{- $v := .value -}}
{{- $s := toString $v -}}
{{- if kindIs "float64" $v -}}
{{- if ne $v (float64 (int64 $v)) -}}
{{- fail (printf "%s is %v -- it takes a whole number" .key $v) -}}
{{- end -}}
{{- $s = toString (int64 $v) -}}
{{- end -}}
{{- if not (has $s (list "" "<nil>")) -}}
{{- if not (regexMatch "^[0-9]+$" $s) -}}
{{- fail (printf "%s is %q -- it takes a whole number" .key $s) -}}
{{- end -}}
{{- if lt (atoi $s) (int .min) -}}
{{- fail (printf "%s is %s -- the engine refuses anything under %d" .key $s (int .min)) -}}
{{- end -}}
{{ $s }}
{{- end -}}
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
dfe-engine.secretsOnAws -- non-empty when the engine keeps what it mints in AWS
Secrets Manager (secrets.provider aws), which layer2-apps.yaml sets from the
cluster secret's engine_secrets_prefix.

Empty keeps the files on the engine's config mount. Any other provider fails
the render: nothing here wires its address or credentials.
*/}}
{{- define "dfe-engine.secretsOnAws" -}}
{{- $provider := toString (dig "provider" "" (.Values.secrets | default dict)) -}}
{{- if eq $provider "aws" -}}
true
{{- else if not (has $provider (list "" "<nil>")) -}}
{{- fail (printf "secrets.provider is %q -- dfe-extras wires aws, or empty for files on the engine's config mount" $provider) -}}
{{- end -}}
{{- end -}}

{{/*
dfe-engine.sidecarServiceAccount -- the account the hunt runner or keda shim runs
as. Takes (dict "ctx" . "name" <workload name>).

On aws the engine's account carries its Secrets Manager write grant through EKS
Pod Identity, so each of these takes an account of its own, rendered by
dfe-engine.sidecarServiceAccountObject, that carries nothing.
*/}}
{{- define "dfe-engine.sidecarServiceAccount" -}}
{{- if include "dfe-engine.secretsOnAws" .ctx -}}
{{ .name }}
{{- else -}}
{{ include "dfe-common.serviceAccountName" .ctx }}
{{- end -}}
{{- end -}}

{{/*
dfe-engine.sidecarServiceAccountObject -- the ServiceAccount
sidecarServiceAccount names on aws, and nothing elsewhere. Takes (dict "ctx" .
"name" <workload name> "component" <app.kubernetes.io/component>).
*/}}
{{- define "dfe-engine.sidecarServiceAccountObject" -}}
{{- if include "dfe-engine.secretsOnAws" .ctx }}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ .name }}
  labels:
    app.kubernetes.io/name: {{ .name }}
    app.kubernetes.io/part-of: {{ .ctx.Values.project | quote }}
    app.kubernetes.io/managed-by: "helm"
    app.kubernetes.io/component: {{ .component }}
    dfe.hyperi.io/env: {{ .ctx.Values.env | quote }}
    dfe.hyperi.io/cloud: {{ .ctx.Values.cloud | quote }}
# Neither workload calls the Kubernetes API.
automountServiceAccountToken: false
---
{{- end }}
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

{{/*
dfe-engine.clickhouseTls -- "true" when the engine and its sidecars dial
ClickHouse over HTTPS, empty otherwise.

clickhouse-cluster's own rule, so both ends agree: on when tls.enabled AND the
server has a CA to sign with (an issuerRef, or the edge module's internal CA),
or when the server is external and brings its own certificate.

A clickhouse.secure or clickhouse.verify key fails the render: nothing reads
either, so a values file setting one would dial plaintext without a word.
*/}}
{{- define "dfe-engine.clickhouseTls" -}}
{{- range $old := list "secure" "verify" -}}
{{- if hasKey $.Values.clickhouse $old -}}
{{- fail (printf "clickhouse.%s is not read -- set clickhouse.tls.%s, the one block clickhouse-cluster, dfe-loader and hyperdx read too" $old (ternary "enabled" "verify" (eq $old "secure"))) -}}
{{- end -}}
{{- end -}}
{{- $tls := .Values.clickhouse.tls | default dict -}}
{{- $signed := or (eq (toString .Values.clickhouse.mode) "external") (dig "issuerRef" "name" "" $tls) (eq (toString (dig "internalCA" "present" false $tls)) "true") -}}
{{- if and (eq (toString $tls.enabled) "true") $signed -}}true{{- end -}}
{{- end -}}

{{/*
dfe-engine.clickhouseCaDir -- where the ClickHouse CA is mounted, or empty when
the image's system roots verify the server. Takes the root context.
*/}}
{{- define "dfe-engine.clickhouseCaDir" -}}
{{- if include "dfe-engine.clickhouseTls" . -}}
{{- $ca := dig "tls" "ca" dict .Values.clickhouse -}}
{{- if and $ca.secretName $ca.configMapName -}}
{{- fail "clickhouse.tls.ca takes a secretName or a configMapName, not both" -}}
{{- end -}}
{{- if or $ca.secretName $ca.configMapName -}}/etc/dfe/clickhouse-ca{{- end -}}
{{- end -}}
{{- end -}}

{{/*
dfe-engine.clickhouseEnv -- where the engine, the hunt runner and the keda shim
reach ClickHouse, and over what. One helper for the three pods, for the reason
clickhouseAuthEnv gives.
*/}}
{{- define "dfe-engine.clickhouseEnv" -}}
{{- $ch := .Values.clickhouse -}}
{{- $tls := include "dfe-engine.clickhouseTls" . -}}
- name: DFE_CLICKHOUSE_HOST
  value: {{ $ch.host | quote }}
- name: DFE_CLICKHOUSE_PORT
  value: {{ ternary $ch.tls.port $ch.port (eq $tls "true") | quote }}
- name: DFE_CLICKHOUSE_DATA_DATABASE
  value: {{ $ch.database | quote }}
{{- /* The engine's own default is secure, so plaintext is stated, not omitted. */}}
- name: DFE_CLICKHOUSE_SECURE
  value: {{ ternary "true" "false" (eq $tls "true") | quote }}
{{- if $tls }}
- name: DFE_CLICKHOUSE_VERIFY
  value: {{ ternary "false" "true" (eq (toString $ch.tls.verify) "false") | quote }}
{{- with (include "dfe-engine.clickhouseCaDir" .) }}
- name: DFE_CLICKHOUSE_CA_CERT
  value: {{ printf "%s/%s" . ($ch.tls.ca.dataKey | default "ca.crt") | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
dfe-engine.clickhouseCaVolume / clickhouseCaMount -- the CA's volume and its
mount, both empty when no CA is mounted. A directory mount, so a rotated CA
reaches the file DFE_CLICKHOUSE_CA_CERT names.
*/}}
{{- define "dfe-engine.clickhouseCaVolume" -}}
{{- if include "dfe-engine.clickhouseCaDir" . -}}
{{- $ca := .Values.clickhouse.tls.ca -}}
{{- $key := $ca.dataKey | default "ca.crt" -}}
- name: clickhouse-ca
  {{- if $ca.secretName }}
  secret:
    secretName: {{ $ca.secretName }}
  {{- else }}
  configMap:
    name: {{ $ca.configMapName }}
  {{- end }}
    items:
      - key: {{ $key }}
        path: {{ $key }}
{{- end -}}
{{- end -}}

{{- define "dfe-engine.clickhouseCaMount" -}}
{{- with (include "dfe-engine.clickhouseCaDir" .) -}}
- name: clickhouse-ca
  mountPath: {{ . }}
  readOnly: true
{{- end -}}
{{- end -}}
