{{/*
dfe-clickhouse.tlsEnabled -- "true" when this chart serves ClickHouse over TLS,
empty otherwise.

On only when clickhouse.tls.enabled is set AND there is a CA to sign with: an
explicit issuerRef, or the edge module's internal CA. A deployment with neither
stays on HTTP rather than waiting on a Certificate nothing can issue. External
mode deploys no server, so it never does. dfe-engine, dfe-loader and hyperdx
apply the same rule to the same block, so both ends agree on the scheme.
*/}}
{{- define "dfe-clickhouse.tlsEnabled" -}}
{{- $tls := .Values.clickhouse.tls | default dict -}}
{{- $issuer := or (dig "issuerRef" "name" "" $tls) (eq (toString (dig "internalCA" "present" false $tls)) "true") -}}
{{- if and (eq (toString $tls.enabled) "true") $issuer (ne .Values.clickhouse.mode "external") -}}true{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.tlsIssuer -- the issuer name the server certificate is requested
from: the explicit one, else the internal CA.
*/}}
{{- define "dfe-clickhouse.tlsIssuer" -}}
{{- $tls := .Values.clickhouse.tls -}}
{{- dig "issuerRef" "name" "" $tls | default (dig "internalCA" "issuerName" "dfe-internal-ca" $tls) -}}
{{- end }}

{{/*
dfe-clickhouse.tlsCaSecret -- the CA-only Secret in this namespace that the app
namespaces copy from, so the Secret holding tls.key is read only from here.
*/}}
{{- define "dfe-clickhouse.tlsCaSecret" -}}
{{- printf "%s-ca" .Values.clickhouse.tls.secretName -}}
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
