{{/*
dfe-engine.env -- the engine container's env entries, less the OTLP endpoint and
service name the thin chart's own env carries (apps/_common.yaml and
scalo-service). dfe-engine-env carries the literal entries, each only while its
condition holds. The secret, ConfigMap and downward-API references stay in the
thin chart's env.
*/}}
{{- define "dfe-engine.env" -}}
            {{- include "dfe-common.versionCheckEnv" (dict "ctx" . "prefix" "DFE_ENGINE_API_") | nindent 12 }}
            # The deployment's posture, the value of the dfe.hyperi.io/env label. The engine defaults to production, which routes
            # every gitcrud write to a review PR. The e2e posture runs as `test`, the one its seed scripts accept.
            - name: DFE_ENGINE_ENV
              value: {{ ternary "test" .Values.env (eq (include "dfe-engine.e2eServer" .) "true") | quote }}
            {{- if include "dfe-engine.e2eServer" . }}
            # The unauthenticated /api/e2e seed routes the dfe-ui suite drives.
            - name: DFE_ENGINE_E2E_SERVER
              value: "true"
            {{- end }}
            # Injected, never detected: the engine defaults to "unknown" and answers every scaling dial with 409 scaling_unsupported.
            - name: DFE_ENGINE_DEPLOYMENT_TARGET
              value: "kubernetes"
            # Rendered by the gateway chart and optional: no gateway means no admin links, not a failed start.
            - name: DFE_ENGINE_ADMIN_LINKS
              valueFrom:
                configMapKeyRef:
                  name: dfe-admin-links
                  key: admin_links.json
                  optional: true
            # From the deployment-wide kafka.mode. Unset, the engine defaults to bus and accepts sources on a transport the
            # brokerless profiles do not run.
            - name: DFE_ENGINE_TRANSPORT_DEFAULT
              value: {{ include "dfe-common.transport" . | quote }}
            - name: DFE_ENGINE_TRANSPORT_BUS_PRESENT
              value: {{ ternary "true" "false" (eq (include "dfe-common.transport" .) "bus") | quote }}
            {{- if (include "dfe-common.transportIsBus" .) }}
            # On the bus the engine creates each source's _land/_load topics, so it dials the broker as the data plane's SASL user.
            - name: DFE_ENGINE_KAFKA_BOOTSTRAP_SERVERS
              value: {{ .Values.kafka.bootstrapServers | quote }}
            # The boot pass that recreates every deployed source's topics runs only when this is true.
            - name: DFE_ENGINE_KAFKA_ENSURE_TOPICS
              value: "true"
            {{- with .Values.kafka.messageMaxBytes }}
            {{- if le (int64 .) 0 }}
            {{- fail (printf "kafka.messageMaxBytes must be a positive whole number of bytes, got %v" .) }}
            {{- end }}
            # Unset leaves the engine's default. int64 because a bare float64 renders in scientific notation.
            - name: DFE_ENGINE_KAFKA_TOPIC_MAX_MESSAGE_BYTES
              value: {{ int64 . | quote }}
            {{- end }}
            {{- with (include "dfe-engine.optionalCount" (dict "key" "kafka.topicReplicationFactor" "value" .Values.kafka.topicReplicationFactor "min" 1)) }}
            # Unset, the engine clamps the dfe-schemas default to the brokers it finds, which a single broker needs.
            - name: DFE_ENGINE_KAFKA_TOPIC_REPLICATION_FACTOR
              value: {{ . | quote }}
            {{- end }}
            {{- with (include "dfe-engine.optionalCount" (dict "key" "kafka.topicRetentionMs" "value" .Values.kafka.topicRetentionMs "min" 1)) }}
            # Unset leaves the broker's default, which on a SaaS broker is the provider's.
            - name: DFE_ENGINE_KAFKA_TOPIC_RETENTION_MS
              value: {{ . | quote }}
            {{- end }}
            {{- if .Values.kafka.saslSecretName }}
            # The kafka.securityProtocol dial every data-plane chart reads, defaulting to the in-cluster plain listener.
            - name: DFE_ENGINE_KAFKA_SECURITY_PROTOCOL
              value: {{ .Values.kafka.securityProtocol | default "SASL_PLAINTEXT" | quote }}
            - name: DFE_ENGINE_KAFKA_SASL_MECHANISM
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.kafka.saslSecretName }}
                  key: sasl.mechanism
            - name: DFE_ENGINE_KAFKA_SASL_USERNAME
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.kafka.saslSecretName }}
                  key: username
            - name: DFE_ENGINE_KAFKA_SASL_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.kafka.saslSecretName }}
                  key: password
            {{- end }}
            {{- end }}
            # Whether the stage pools sit behind a listener, and where. The engine addresses a pool the way the charts front it
            # (apps.yaml mesh.host_pattern). Absent means the pools are dialled directly, which is every profile but mesh.
            - name: DFE_ENGINE_MESH_ENABLED
              value: {{ ternary "true" "false" (eq (include "dfe-common.meshEnabled" .) "true") | quote }}
            {{- if (include "dfe-common.meshEnabled" .) }}
            - name: DFE_ENGINE_MESH_NAMESPACE
              value: {{ .Values.mesh.gateway.namespace | quote }}
            {{- end }}
            {{- with .Values.stackVersion }}
            # From the cluster secret's stack_version annotation. GET /api/v1/system/version prefers the deploy repo's pins.yaml
            # and falls back to this.
            - name: DFE_ENGINE_STACK_VERSION
              value: {{ . | quote }}
            {{- end }}
            {{- with .Values.profile }}
            # The tier, from the cluster secret annotation that selects profile-<x>.yaml. Without it the console lists every
            # optional app.
            - name: DFE_ENGINE_PROFILE
              value: {{ . | quote }}
            {{- end }}
            {{- with .Values.uiVersion }}
            # The dfe-ui pin, reported when the deploy repo's pins name no ui override.
            - name: DFE_ENGINE_UI_VERSION
              value: {{ . | quote }}
            {{- end }}
            # Reconciles the ClickHouse tenant fence on startup: tier roles, _org_id row policies, deny policies and a user per
            # org/group. False only for a deployment with no tenant isolation.
            - name: DFE_ENGINE_TENANT_ISOLATION_ENABLED
              value: {{ .Values.tenantIsolation.enabled | quote }}
            # The settings loader reads DFE_CLICKHOUSE_* (or legacy CLICKHOUSE_*), not DFE_ENGINE_*, which it ignores and leaves
            # ClickHouse at localhost:9000. It connects to the `default` db, and the startup bootstrap creates the data database.
            {{- include "dfe-engine.clickhouseEnv" . | nindent 12 }}
            # Default TTL in days for every time-series table, 0 = none. A source or dfe-schemas TTL overrides it.
            - name: DFE_ENGINE_CLICKHOUSE_DEFAULT_TTL_DAYS
              value: {{ .Values.retention.defaultTtlDays | quote }}
            {{- include "dfe-engine.clickhouseAuthEnv" . | nindent 12 }}
            {{- if .Values.huntRunner.enabled }}
            # The password the engine gives dfe_hunt_runner, from the runner's own Secret so the two pods never differ.
            - name: DFE_ENGINE_CLICKHOUSE_HUNT_RUNNER_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.huntRunner.clickhouse.passwordSecretName }}
                  key: {{ .Values.huntRunner.clickhouse.passwordSecretKey }}
            {{- end }}
            # Without these the engine leaves every registry uninitialised and the config API answers 503 not_configured. They sit
            # under the writable config mount, where the engine seeds defaults and does git-native CRUD.
            - name: DFE_ENGINE_SOURCES_DIR
              value: {{ .Values.config.mountPath }}/sources
            - name: DFE_ENGINE_SERVICES_CONFIG_YAML_DIR
              value: {{ .Values.config.mountPath }}/services
            # The mounted manifest the management API reflects, so an app change needs no engine release.
            - name: DFE_ENGINE_APP_CATALOGUE_FILE
              value: /etc/dfe-engine/catalogue/apps.yaml
            # Unconditional: an absent directory is a no-op, so a content entry added later needs no env change.
            - name: DFE_ENGINE_LIBRARY_SEED_DIR
              value: {{ .Values.content.mountPath }}/library
            # Where the settings API reads each app's config schema and capabilities. Unconditional for the same reason.
            - name: DFE_ENGINE_APP_CONTRACT_DIR
              value: {{ .Values.content.mountPath }}/contract
            - name: DFE_ENGINE_SCHEMAS_DIR
              value: {{ .Values.config.mountPath }}/schemas
            - name: DFE_ENGINE_FIELDMAPS_DIR
              value: {{ .Values.config.mountPath }}/fieldmaps
            - name: DFE_ENGINE_HUNTS_DIR
              value: {{ .Values.config.mountPath }}/hunts
            - name: DFE_ENGINE_HUNTS_RULES_DIR
              value: {{ .Values.config.mountPath }}/rules
            - name: DFE_ENGINE_HUNTS_ALERT_DESTINATIONS_DIR
              value: {{ .Values.config.mountPath }}/alerts
            {{- include "dfe-engine.huntSmoothingEnv" . | nindent 12 }}
            # The shared signing key: the engine adopts it, as its sidecar pods do, instead of minting a private one.
            - name: DFE_ENGINE_API_JWT_SECRET
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.auth.jwtSecretName }}
                  key: {{ .Values.auth.jwtSecretKey }}
            {{- with (.Values.api.forwardedAllowIps | default (dig "podCIDR" "" (.Values.networkModel | default dict))) }}
            # The proxy hops whose X-Forwarded-* are believed, defaulting to the gateway and dfe-ui pod range. Unset, OIDC
            # callbacks go out as http and every request audits as the gateway's address.
            - name: DFE_ENGINE_API_FORWARDED_ALLOW_IPS
              value: {{ . | quote }}
            {{- end }}
            {{- with .Values.api.jwtExpireMinutes }}
            # How long an issued token stays valid. Unset leaves the engine's 60 minutes.
            - name: DFE_ENGINE_API_JWT_EXPIRE_MINUTES
              value: {{ . | quote }}
            {{- end }}
            {{- with (include "dfe-engine.optionalCount" (dict "key" "api.maxSessionMinutes" "value" .Values.api.maxSessionMinutes "min" 1)) }}
            # A refresh never extends a token past this many minutes from sign-in. Unset leaves the engine's 720.
            - name: DFE_ENGINE_API_MAX_SESSION_MINUTES
              value: {{ . | quote }}
            {{- end }}
            {{- with (include "dfe-engine.docsEnabled" .) }}
            # Unset serves /docs and /redoc on a dev posture only; the gateway
            # and the links page read the same key.
            - name: DFE_ENGINE_API_DOCS_ENABLED
              value: {{ . | quote }}
            {{- end }}
            {{- with .Values.auth.proxyProvider }}
            # The provider a gateway login is stamped with, the only one whose group links its asserted groups reach.
            # Unset leaves `oidc`.
            - name: DFE_ENGINE_AUTH_PROXY_PROVIDER
              value: {{ . | quote }}
            {{- end }}
            {{- with (include "dfe-engine.optionalCount" (dict "key" "auth.apiKeyDefaultTtlDays" "value" .Values.auth.apiKeyDefaultTtlDays "min" 0)) }}
            # 0 is a key with no expiry, so the test is empty, not falsy.
            - name: DFE_ENGINE_AUTH_API_KEY_DEFAULT_TTL_DAYS
              value: {{ . | quote }}
            {{- end }}
            {{- with .Values.auth.loginThrottle }}
            {{- with (include "dfe-engine.optionalBool" (dict "key" "auth.loginThrottle.enabled" "value" .enabled)) }}
            - name: DFE_ENGINE_AUTH_LOGIN_THROTTLE_ENABLED
              value: {{ . | quote }}
            {{- end }}
            {{- with (include "dfe-engine.optionalCount" (dict "key" "auth.loginThrottle.usernameFailures" "value" .usernameFailures "min" 1)) }}
            - name: DFE_ENGINE_AUTH_LOGIN_THROTTLE_USERNAME_FAILURES
              value: {{ . | quote }}
            {{- end }}
            {{- with (include "dfe-engine.optionalCount" (dict "key" "auth.loginThrottle.clientFailures" "value" .clientFailures "min" 1)) }}
            - name: DFE_ENGINE_AUTH_LOGIN_THROTTLE_CLIENT_FAILURES
              value: {{ . | quote }}
            {{- end }}
            {{- with (include "dfe-engine.optionalCount" (dict "key" "auth.loginThrottle.maxDelaySeconds" "value" .maxDelaySeconds "min" 1)) }}
            - name: DFE_ENGINE_AUTH_LOGIN_THROTTLE_MAX_DELAY_SECONDS
              value: {{ . | quote }}
            {{- end }}
            {{- end }}
            {{- if include "dfe-engine.secretsOnAws" . }}
            {{- $store := .Values.secrets }}
            {{- $window := int64 ($store.recoveryWindowDays | default 0) }}
            {{- if and (ne $window 0) (or (lt $window 7) (gt $window 30)) }}
            {{- fail (printf "secrets.recoveryWindowDays is %d -- Secrets Manager takes 0 (no window) or 7-30" $window) }}
            {{- end }}
            # Secrets the engine mints (JWT key, ClickHouse passwords, OIDC client secrets) go to Secrets Manager under the
            # prefix the aws root fenced its role to, through EKS Pod Identity.
            {{- include "dfe-engine.secretsStoreEnv" . | nindent 12 }}
            # Empty would fall back to the account's aws/secretsmanager key, outside the deployment's.
            - name: DFE_ENGINE_SECRETS_KMS_KEY_ID
              value: {{ required "secrets.kmsKeyId is empty: with secrets.provider aws the engine needs the deployment's key, the cluster secret's dfe.hyperi.io/kms_key_arn annotation" $store.kmsKeyId | quote }}
            - name: DFE_ENGINE_SECRETS_RECOVERY_WINDOW_DAYS
              value: {{ $window | quote }}
            - name: DFE_ENGINE_SECRETS_TAGS
              value: {{ $store.tags | default dict | toJson | quote }}
            {{- else }}
            {{- include "dfe-engine.secretsStoreEnv" . | nindent 12 }}
            # No receiver or runner pod can mount that volume, so the engine also
            # writes dfe-receiver's bearer tokens and the hunt runner's alert
            # destinations into Secrets they mount (engine-ingest-bearer-secret.yaml,
            # engine-alert-destinations-secret.yaml).
            - name: DFE_ENGINE_INGEST_BEARER_SECRET
              value: {{ .Values.ingestBearer.secretName | quote }}
            - name: DFE_ENGINE_HUNTS_ALERT_DESTINATIONS_SECRET
              value: {{ .Values.alertDestinations.secretName | quote }}
            {{- end }}
            # Without this the auth bootstrap falls back to ./config/auth under the read-only /app WORKDIR and the pod crashloops
            # (dfe-engine#106). The mount is the dfe-engine-config PVC, so the stores outlive a restart, and a second replica
            # needs storage it can share.
            - name: DFE_ENGINE_AUTH_DIR
              value: {{ .Values.config.mountPath }}/auth
            # The same fallback for every other startup mkdir (the org and service-surface registries, rbac/connections.yaml):
            # the shared root points at the writable config mount.
            - name: DFE_CONFIG_DIR
              value: {{ .Values.config.mountPath }}
            {{- if gt (len .Values.seedAuth.seedAccounts) 0 }}
            # Stable named team logins, reconciled on every boot with config winning. Referenced only when the overlay supplies
            # accounts, so an absent Secret never holds the pod in CreateContainerConfigError.
            - name: DFE_ENGINE_AUTH_LOCAL_SEED_ACCOUNTS
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.seedAuth.secretName }}
                  key: seed-accounts
            {{- end }}
            {{- if .Values.auth.adminSecretName }}
            # The minted admin password, reasserted on every boot. The engine refuses to start on an unset or `changeme` value
            # unless env is a dev posture. An empty adminSecretName is the dev-only no-Secret case.
            - name: DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.auth.adminSecretName }}
                  key: {{ .Values.auth.adminSecretKey }}
            # The engine composes the login page's credential-fetch command from
            # these and the namespace below.
            - name: DFE_ENGINE_AUTH_LOCAL_ADMIN_SECRET_NAME
              value: {{ .Values.auth.adminSecretName | quote }}
            - name: DFE_ENGINE_AUTH_LOCAL_ADMIN_SECRET_KEY
              value: {{ .Values.auth.adminSecretKey | quote }}
            {{- end }}
            - name: DFE_ENGINE_DEPLOYMENT_NAMESPACE
              valueFrom:
                fieldRef:
                  fieldPath: metadata.namespace
            {{- with .Values.auth.adminSecretPath }}
            # Set = the rotate-password endpoint writes through the scalo secrets
            # provider instead of answering 501 with the kubectl patch command.
            - name: DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD_SECRET_PATH
              value: {{ . | quote }}
            {{- end }}
            {{- if .Values.auth.breakglassSecretName }}
            # First-boot break-glass password: the engine hashes it into the
            # deploy repo once and ignores it afterwards.
            - name: DFE_ENGINE_AUTH_BREAKGLASS_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.auth.breakglassSecretName }}
                  key: {{ .Values.auth.breakglassSecretKey }}
            {{- end }}
          {{- if .Values.gitops.enabled }}
            - name: DFE_ENGINE_GITOPS_ENABLED
              value: "true"
            - name: DFE_ENGINE_GITOPS_REPO_URL
              value: {{ .Values.gitops.repoUrl | quote }}
            - name: DFE_ENGINE_GITOPS_BRANCH
              value: {{ .Values.gitops.branch | quote }}
            - name: DFE_ENGINE_GITOPS_LOCAL_PATH
              value: {{ .Values.gitops.localPath | quote }}
            - name: DFE_ENGINE_GITOPS_USERNAME
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.gitops.credentialsSecret }}
                  key: username
            - name: DFE_ENGINE_GITOPS_TOKEN
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.gitops.credentialsSecret }}
                  key: password
          {{- end }}
          {{- if .Values.hyperdx.enabled }}
            # Machine-JWT control surface: the engine confirms the fork's default team and seeds connections.
            - name: DFE_ENGINE_HYPERDX_ENABLED
              value: "true"
            - name: DFE_ENGINE_HYPERDX_BASE_URL
              value: {{ include "dfe-engine.hyperdxBaseUrl" . | quote }}
          {{- end }}
          {{- if .Values.auth.trustProxyHeaders }}
            # The engine believes X-Oidc-* only where a gateway both strips the inbound values and injects verified ones. Its
            # default is false: see the value's comment for what has to be true before setting it.
            - name: DFE_ENGINE_AUTH_TRUST_PROXY_AUTH_HEADERS
              value: "true"
          {{- end }}
          {{- if .Values.oidc.enabled }}
          {{- range $provider := .Values.oidc.providers }}
          {{- range $envVar, $secretKey := $provider.envMappings }}
            - name: {{ $envVar }}
              valueFrom:
                secretKeyRef:
                  name: {{ $provider.secretName }}
                  key: {{ $secretKey }}
          {{- end }}
          {{- end }}
          {{- end }}
          {{- if .Values.authConfig.caBundleConfigMap }}
            # Private-CA IdPs and datastores verify against this bundle: the init container's merge of the configured CA and the
            # image's public roots, not the ConfigMap itself.
            - name: SSL_CERT_FILE
              value: {{ .Values.config.mountPath }}/auth/ca-bundle.pem
          {{- end }}
{{- end -}}
