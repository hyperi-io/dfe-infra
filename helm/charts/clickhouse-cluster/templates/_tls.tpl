{{/*
dfe-clickhouse.tlsEnabled -- "true" when this chart serves ClickHouse over TLS, empty otherwise.

On when clickhouse.tls.enabled is set and there is a CA to sign with. The clients apply the same rule to the same block. docs/deploy-tls-trust.md, In-cluster ClickHouse.
*/}}
{{- define "dfe-clickhouse.tlsEnabled" -}}
{{- $tls := .Values.clickhouse.tls | default dict -}}
{{- $issuer := or (dig "issuerRef" "name" "" $tls) (eq (toString (dig "internalCA" "present" false $tls)) "true") -}}
{{- if and (eq (toString $tls.enabled) "true") $issuer (ne .Values.clickhouse.mode "external") -}}true{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.tlsIssuer -- the issuer the certificate is requested from: the explicit one, else the internal CA.
*/}}
{{- define "dfe-clickhouse.tlsIssuer" -}}
{{- $tls := .Values.clickhouse.tls -}}
{{- dig "issuerRef" "name" "" $tls | default (dig "internalCA" "issuerName" "dfe-internal-ca" $tls) -}}
{{- end }}

{{/*
dfe-clickhouse.tlsCaSecret -- the CA-only Secret the app namespaces copy from.
*/}}
{{- define "dfe-clickhouse.tlsCaSecret" -}}
{{- printf "%s-ca" .Values.clickhouse.tls.secretName -}}
{{- end }}

{{/*
dfe-clickhouse.clientService -- the Service a client dials: the StatefulSet's in single mode, the operator's headless one in cluster mode.
*/}}
{{- define "dfe-clickhouse.clientService" -}}
{{- if eq .Values.clickhouse.mode "cluster" -}}
{{- printf "%s-clickhouse-headless" .Values.clickhouse.name -}}
{{- else -}}
{{- .Values.clickhouse.name -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.tlsDnsNames -- every name on the certificate, one per line: the client Service in each form, its per-pod names, localhost, then clickhouse.tls.dnsNames.
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
dfe-clickhouse.validateTls -- refuse a TLS setting this deployment cannot honour.
*/}}
{{- define "dfe-clickhouse.validateTls" -}}
{{- $tls := .Values.clickhouse.tls | default dict -}}
{{- if and (eq (toString $tls.required) "true") (ne .Values.clickhouse.mode "external") (not (include "dfe-clickhouse.tlsEnabled" .)) -}}
{{- fail "clickhouse.tls.required needs TLS on -- clickhouse.tls.enabled and a CA to sign with (clickhouse.tls.issuerRef, or the edge module's internal CA) -- or it closes the plaintext ports and leaves none" -}}
{{- end -}}
{{- if include "dfe-clickhouse.tlsEnabled" . -}}
{{- if not $tls.secretName -}}
{{- fail "clickhouse.tls.enabled needs clickhouse.tls.secretName -- the Secret cert-manager writes the server keypair to" -}}
{{- end -}}
{{- if and (eq (toString $tls.required) "true") (eq .Values.clickhouse.mode "single") -}}
{{- fail "clickhouse.tls.required is cluster mode only -- the single-node server keeps 8123 for its own probes" -}}
{{- end -}}
{{- end -}}
{{- end }}
