{{/*
The engine's JWKS endpoint, which oidc-proxy mode verifies every token against.

An explicit dfeAuth.engineJwksUrl wins, for an engine outside the cluster. Otherwise
it is derived from the engine's service, defaulting to this release's namespace so a
co-deployed engine needs no configuration at all.
*/}}
{{- define "hyperdx.engineJwksUrl" -}}
{{- with .Values.dfeAuth.engineJwksUrl -}}
{{ . }}
{{- else -}}
{{- $ns := .Values.dfeAuth.engineNamespace | default .Release.Namespace -}}
{{- printf "http://%s.%s.svc.cluster.local:%v/.well-known/jwks.json" .Values.dfeAuth.engineService $ns .Values.dfeAuth.enginePort -}}
{{- end -}}
{{- end -}}

{{/*
hyperdx.clickhouseCa -- "true" when a ClickHouse CA is mounted for Node to
trust, empty otherwise.

TLS follows clickhouse-cluster's rule, so the CA is mounted exactly when the
engine hands out an https connection: tls.enabled AND a CA to sign with (an
issuerRef, or the edge module's internal CA), or an external server.
*/}}
{{- define "hyperdx.clickhouseCa" -}}
{{- $tls := .Values.clickhouse.tls | default dict -}}
{{- $signed := or (eq (toString .Values.clickhouse.mode) "external") (dig "issuerRef" "name" "" $tls) (eq (toString (dig "internalCA" "present" false $tls)) "true") -}}
{{- if and (eq (toString $tls.enabled) "true") $signed -}}
{{- $ca := $tls.ca | default dict -}}
{{- if and $ca.secretName $ca.configMapName -}}
{{- fail "clickhouse.tls.ca takes a secretName or a configMapName, not both" -}}
{{- end -}}
{{- if or $ca.secretName $ca.configMapName -}}true{{- end -}}
{{- end -}}
{{- end -}}

{{/*
hyperdx.env -- the hyperdx container's env list, as the 2.2.0 deployment wrote
it less three entries: OTEL_EXPORTER_OTLP_ENDPOINT, which the thin chart's env
sets from telemetry.mode, and CLICKHOUSE_PORT and CLICKHOUSE_DB, which HyperDX
never reads (CLICKHOUSE_HOST carries the port). dfe-hyperdx-env carries its
literal entries; the secret references and MONGO_URI, which the kubelet
expands, stay in the thin chart's env.
*/}}
{{- define "hyperdx.env" -}}
{{- $clickhouseCa := include "hyperdx.clickhouseCa" . }}
            # OFF unless a deployer opts in. HyperDX's usage-stats task defaults to
            # ON (its config.ts reads `env.USAGE_STATS_ENABLED !== 'false'`) and ships
            # upstream's own ingest key, so an unset value means this pod reports team
            # /user/source counts and hostnames to https://in-otel.hyperdx.io -- a
            # THIRD PARTY. DFE is a product other organisations deploy into their own
            # estates; their telemetry is theirs, and nobody opted into that by
            # installing DFE. The stack already self-monitors into the deployer's own
            # ClickHouse via the telemetry seam, so this buys them nothing.
            # Deployers who WANT to support upstream can set usageStats.enabled: true.
            - name: USAGE_STATS_ENABLED
              value: {{ .Values.usageStats.enabled | default false | quote }}
            # A URL: HyperDX refuses webhooks aimed at this host:port only when the value parses as one, and a bare host name registers no refusal at all.
            - name: CLICKHOUSE_HOST
              value: {{ printf "http://%s:%v" .Values.clickhouse.host .Values.clickhouse.port | quote }}
            {{- if .Values.clickhouse.user }}
            - name: CLICKHOUSE_USER
              value: {{ .Values.clickhouse.user }}
            {{- end }}
            {{- if .Values.clickhouse.passwordSecretName }}
            # Only reference a secret that the deploy actually creates. Referencing an
            # absent secret leaves the pod in CreateContainerConfigError forever, which
            # reads as "hyperdx is broken" rather than "this deploy has no CH user".
            - name: CLICKHOUSE_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.clickhouse.passwordSecretName }}
                  key: {{ .Values.clickhouse.passwordSecretKey | default "password" }}
            {{- end }}
            {{- if $clickhouseCa }}
            # Added to Node's bundled roots, not in place of them, for every
            # process in the image: the ClickHouse client and proxy verify 8443.
            - name: NODE_EXTRA_CA_CERTS
              value: {{ printf "/etc/dfe/clickhouse-ca/%s" (.Values.clickhouse.tls.ca.dataKey | default "ca.crt") | quote }}
            {{- end }}
            {{- if .Values.mongodb.passwordSecretName }}
            # Declared BEFORE MONGO_URI: kubelet $(VAR) expansion only reads
            # previously-declared env. Only referenced when the deploy actually
            # projects the secret (same guard as CLICKHOUSE_PASSWORD above).
            - name: MONGO_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.mongodb.passwordSecretName }}
                  key: {{ .Values.mongodb.passwordSecretKey | default "password" }}
            {{- end }}
            - name: MONGO_URI
              value: {{ .Values.mongodb.uri | quote }}
            # Unset, the image signs sessions with a key published in upstream's source.
            - name: EXPRESS_SESSION_SECRET
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.sessionSecret.name }}
                  key: {{ .Values.sessionSecret.key }}
            # Unset, the image stores third-party tokens in plain text.
            - name: TOKEN_ENCRYPTION_KEY
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.tokenEncryption.name }}
                  key: {{ .Values.tokenEncryption.key }}
            {{- if .Values.dashboards.enabled }}
            # Setting the directory is what starts the provisioner: entry.prod.sh
            # launches the provision-dashboards task only when it is present.
            - name: DASHBOARD_PROVISIONER_DIR
              value: {{ .Values.dashboards.mountPath | quote }}
            {{- if .Values.dashboards.allTeams }}
            - name: DASHBOARD_PROVISIONER_ALL_TEAMS
              value: "true"
            {{- end }}
            {{- if .Values.dashboards.requireRefs }}
            - name: DASHBOARD_PROVISIONER_REQUIRE_REFS
              value: "true"
            {{- end }}
            {{- end }}
            {{- if .Values.dfeAuth.enabled }}
            - name: DFE_AUTH_MODE
              value: {{ .Values.dfeAuth.mode | quote }}
            {{- if eq .Values.dfeAuth.mode "oidc-proxy" }}
            # Required in this mode: the fork throws without it and the middleware
            # turns that into a silent fall-through, so every request 401s.
            - name: DFE_ENGINE_JWKS_URL
              value: {{ include "hyperdx.engineJwksUrl" . | quote }}
            {{- with .Values.dfeAuth.engineIssuer }}
            - name: DFE_ENGINE_ISSUER
              value: {{ . | quote }}
            {{- end }}
            {{- else }}
            # header-dev only; oidc-proxy ignores these entirely.
            - name: DFE_AUTH_HEADER_EMAIL
              value: {{ .Values.dfeAuth.headerEmail | quote }}
            - name: DFE_AUTH_HEADER_GROUPS
              value: {{ .Values.dfeAuth.headerGroups | quote }}
            {{- end }}
            - name: DFE_AUTH_DEFAULT_TEAM
              value: {{ .Values.dfeAuth.defaultTeam | quote }}
            {{- end }}
            {{- if .Values.localAuthPages.enabled }}
            # Opt-in local login/register pages until the deployment's OIDC
            # wiring lands; read per request, so no image rebuild.
            - name: DFE_LOCAL_AUTH_PAGES
              value: "true"
            {{- end }}
            # Post-login redirects and the session cookie anchor on this URL;
            # the image's localhost default breaks both behind the gateway.
            - name: FRONTEND_URL
              {{- /* No domain means no derived URL: "https://hyperdx." would anchor redirects and the session cookie on a host that does not resolve, and the image's own localhost default is closer to right in that deployment. */}}
              value: {{ .Values.frontendUrl | default (empty .Values.domain | ternary "" (printf "https://%s.%s" (coalesce .Values.hyperdxHostname (dig "hyperdx" "hyperdx" (.Values.hostnames | default dict))) .Values.domain)) | quote }}
            # Link-back origin so the embedded HyperDX opens created rules in the
            # DFE UI; needed when dfe-ui and HyperDX are on separate subdomains.
            # next-runtime-env reads it per request, so no image rebuild.
            - name: NEXT_PUBLIC_DFE_UI_BASE_URL
              value: {{ .Values.dfeUiBaseUrl | default (empty .Values.domain | ternary "" (printf "https://%s.%s" (coalesce .Values.dfeHostname (dig "dfe" "dfe" (.Values.hostnames | default dict))) .Values.domain)) | quote }}
            {{- if .Values.defaultConnections.enabled }}
            - name: DEFAULT_CONNECTIONS
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.defaultConnections.secretName }}
                  key: DEFAULT_CONNECTIONS
            # Optional sibling: sources bootstrap through the same seeding path.
            - name: DEFAULT_SOURCES
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.defaultConnections.secretName }}
                  key: DEFAULT_SOURCES
                  optional: true
            {{- end }}
{{- end -}}
