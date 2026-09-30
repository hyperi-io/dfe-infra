{{/*
dfe-clickhouse.tlsEnabled -- "true" when this chart serves ClickHouse over TLS,
empty otherwise. External mode deploys no server, so it never does.
*/}}
{{- define "dfe-clickhouse.tlsEnabled" -}}
{{- $tls := .Values.clickhouse.tls | default dict -}}
{{- if and $tls.enabled (ne .Values.clickhouse.mode "external") -}}true{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.clientService -- the Service a client dials: the StatefulSet's
own in single mode, the operator's headless one in cluster mode. The operator
emits no other client Service (argocd/values/profile-scale.yaml).
*/}}
{{- define "dfe-clickhouse.clientService" -}}
{{- if eq .Values.clickhouse.mode "cluster" -}}
{{- printf "%s-clickhouse-headless" .Values.clickhouse.name -}}
{{- else -}}
{{- .Values.clickhouse.name -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.tlsDnsNames -- every name the server certificate carries, one per
line: each form of the client Service a resolver accepts, the per-pod names
under it, localhost for clients inside the pod, then clickhouse.tls.dnsNames.
*/}}
{{- define "dfe-clickhouse.tlsDnsNames" -}}
{{- $svc := include "dfe-clickhouse.clientService" . -}}
{{- $ns := .Release.Namespace -}}
{{- $names := list $svc (printf "%s.%s" $svc $ns) (printf "%s.%s.svc" $svc $ns) (printf "%s.%s.svc.cluster.local" $svc $ns) (printf "*.%s.%s.svc" $svc $ns) (printf "*.%s.%s.svc.cluster.local" $svc $ns) "localhost" -}}
{{- range (concat $names (.Values.clickhouse.tls.dnsNames | default list) | uniq) }}
{{ . }}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.validateTls -- refuse a TLS setting that would leave the server
without a certificate, or a switch this mode cannot honour.
*/}}
{{- define "dfe-clickhouse.validateTls" -}}
{{- $tls := .Values.clickhouse.tls | default dict -}}
{{- if and $tls.required (not $tls.enabled) -}}
{{- fail "clickhouse.tls.required needs clickhouse.tls.enabled -- it closes the plaintext ports, leaving none" -}}
{{- end -}}
{{- if include "dfe-clickhouse.tlsEnabled" . -}}
{{- if not (dig "issuerRef" "name" "" $tls) -}}
{{- fail "clickhouse.tls.enabled needs clickhouse.tls.issuerRef.name -- the cert-manager issuer that signs the server certificate" -}}
{{- end -}}
{{- if not $tls.secretName -}}
{{- fail "clickhouse.tls.enabled needs clickhouse.tls.secretName -- the Secret cert-manager writes the server keypair to" -}}
{{- end -}}
{{- if and $tls.required (eq .Values.clickhouse.mode "single") -}}
{{- fail "clickhouse.tls.required is cluster mode only -- the single-node server keeps 8123 for its own probes" -}}
{{- end -}}
{{- end -}}
{{- end }}
