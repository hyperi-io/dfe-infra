{{/*
dex.issuerUrl -- the OIDC issuer. Explicit issuer.url wins; else
https://{hostnames.auth}.{domain}; else (no domain) the in-cluster Service URL,
a debug-only path never a product face. domain arrives as the appset param.
*/}}
{{- define "dex.issuerUrl" -}}
{{- if .Values.issuer.url -}}
{{- .Values.issuer.url -}}
{{- else -}}
{{- $domain := .Values.domain | default "" -}}
{{- $host := .Values.hostnames.auth | default "auth" -}}
{{- if $domain -}}
{{- printf "https://%s.%s" $host $domain -}}
{{- else -}}
{{- printf "http://%s.%s.svc.cluster.local:%v" (include "dfe-common.fullname" .) .Release.Namespace .Values.service.httpPort -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
dex.grpcTlsSecret -- the Secret holding the gRPC server cert (tls.crt/tls.key/ca.crt).
existingSecret wins; else the cert-manager-issued {fullname}-grpc-tls.
*/}}
{{- define "dex.grpcTlsSecret" -}}
{{- .Values.grpc.tls.existingSecret | default (printf "%s-grpc-tls" (include "dfe-common.fullname" .)) -}}
{{- end -}}

{{/*
dex.config -- renders dex's config.yaml from values. Delivered as a Secret; the
deployment checksums it so a config change rolls the pods.
*/}}
{{- define "dex.config" -}}
issuer: {{ include "dex.issuerUrl" . }}
storage:
  type: {{ .Values.storage.type }}
{{- if eq .Values.storage.type "kubernetes" }}
  config:
    inCluster: true
{{- else if eq .Values.storage.type "sqlite3" }}
  config:
    file: {{ .Values.storage.sqlite.file }}
{{- end }}
web:
  http: 0.0.0.0:{{ .Values.service.httpPort }}
{{- if .Values.grpc.enabled }}
grpc:
  addr: 0.0.0.0:{{ .Values.service.grpcPort }}
  tlsCert: /etc/dex/grpc/tls.crt
  tlsKey: /etc/dex/grpc/tls.key
  tlsClientCA: /etc/dex/grpc/ca.crt
  reflection: {{ .Values.grpc.reflection }}
{{- end }}
telemetry:
  http: 0.0.0.0:{{ .Values.service.telemetryPort }}
oauth2:
  skipApprovalScreen: true
  passwordConnector: local
{{- if eq .Values.signer.type "vault" }}
signer:
  type: vault
  config:
    keyName: {{ .Values.signer.vault.keyName | quote }}
    {{- with .Values.signer.vault.addr }}
    addr: {{ . | quote }}
    {{- end }}
{{- end }}
enablePasswordDB: true
{{- if .Values.clients.envoy.enabled }}
{{- $uris := .Values.clients.envoy.redirectURIs }}
{{- if not $uris }}
{{- $domain := .Values.domain | default "" }}
{{- if $domain }}
{{- $uris = list (printf "https://%s.%s/oauth2/callback" (.Values.hostnames.dfe | default "dfe") $domain) }}
{{- end }}
{{- end }}
staticClients:
  - id: {{ .Values.clients.envoy.id | quote }}
    name: {{ .Values.clients.envoy.name | quote }}
    secretEnv: DEX_ENVOY_CLIENT_SECRET
    redirectURIs:
{{- range $uris }}
      - {{ . | quote }}
{{- end }}
{{- end }}
frontend:
  issuer: {{ .Values.frontend.issuer | default "DFE" | quote }}
{{- with .Values.frontend.theme }}
  theme: {{ . | quote }}
{{- end }}
{{- with .Values.frontend.dir }}
  dir: {{ . | quote }}
{{- end }}
{{- end -}}
