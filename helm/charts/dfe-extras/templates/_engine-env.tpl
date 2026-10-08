{{/*
dfe-engine.env -- the engine container's env list, as the 2.2.0 deployment wrote
it. dfe-engine-env carries its literal entries; the secret, ConfigMap and
downward-API references stay in the thin chart's env.
*/}}
{{- define "dfe-engine.env" -}}
            {{- include "dfe-common.versionCheckEnv" (dict "ctx" . "prefix" "") | nindent 12 }}
            # Declare the deployment's posture to the engine. WITHOUT this the
            # engine's `env` defaults to "production" (settings.py), so a dev
            # cluster silently self-identifies as prod: every gitcrud write is
            # routed to a review PR (commit_policy.resolve_mode -> "pr" for any
            # non-dev posture) instead of committing to the deploy repo. The
            # deployment already declares its posture -- pass the SAME value the
            # dfe.hyperi.io/env label carries, so the two cannot drift.
            # Generic, not a dev shortcut: a customer's env: production still
            # gets the PR flow, which is the point.
            # The e2e posture runs the engine as `test`, the one posture its
            # seed scripts accept.
            - name: DFE_ENV
              value: {{ ternary "test" .Values.env (eq (include "dfe-engine.e2eServer" .) "true") | quote }}
            {{- if include "dfe-engine.e2eServer" . }}
            # Mounts the unauthenticated /api/e2e seed routes the dfe-ui suite
            # drives (e2eServer).
            - name: DFE_E2E_SERVER
              value: "true"
            {{- end }}
            # Injected by the deployer, never detected: the engine defaults
            # deployment.target to "unknown" and refuses every scaling dial
            # there with 409 scaling_unsupported.
            - name: DFE_DEPLOYMENT_TARGET
              value: "kubernetes"
            # Rendered by the gateway chart beside the engine, and optional, so an
            # engine with no gateway starts with no admin links rather than not at all.
            - name: DFE_ADMIN_LINKS
              valueFrom:
                configMapKeyRef:
                  name: dfe-admin-links
                  key: admin_links.json
                  optional: true
            # What this deployment can carry a source's records on, from the one
            # deployment-wide kafka.mode the profile sets. Without it the engine
            # defaults to bus and accepts sources on a transport the brokerless
            # profiles do not run.
            - name: DFE_TRANSPORT_DEFAULT
              value: {{ include "dfe-common.transport" . | quote }}
            - name: DFE_TRANSPORT_BUS_PRESENT
              value: {{ ternary "true" "false" (eq (include "dfe-common.transport" .) "bus") | quote }}
            {{- if (include "dfe-common.transportIsBus" .) }}
            # On the bus the engine creates each source's _land/_load topics, so
            # it dials the broker with the same SASL user the data plane uses
            # (settings.py: DFE_KAFKA_*; the provider derives the mechanism).
            - name: DFE_KAFKA_BOOTSTRAP_SERVERS
              value: {{ .Values.kafka.bootstrapServers | quote }}
            # The boot pass that recreates every deployed source's topics runs
            # only when this is explicitly true.
            - name: DFE_KAFKA_ENSURE_TOPICS
              value: "true"
            {{- with .Values.kafka.messageMaxBytes }}
            {{- if le (int64 .) 0 }}
            {{- fail (printf "kafka.messageMaxBytes must be a positive whole number of bytes, got %v" .) }}
            {{- end }}
            # Every topic the engine creates takes this max.message.bytes; unset
            # leaves the engine's own default. int64 because a bare float64
            # renders in scientific notation.
            - name: DFE_KAFKA_TOPIC_MAX_MESSAGE_BYTES
              value: {{ int64 . | quote }}
            {{- end }}
            {{- if .Values.kafka.saslSecretName }}
            # The same kafka.securityProtocol dial every data-plane chart reads,
            # defaulting to the in-cluster brokers' plain listener.
            - name: DFE_KAFKA_SECURITY_PROTOCOL
              value: {{ .Values.kafka.securityProtocol | default "SASL_PLAINTEXT" | quote }}
            - name: DFE_KAFKA_SASL_MECHANISM
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.kafka.saslSecretName }}
                  key: sasl.mechanism
            - name: DFE_KAFKA_SASL_USERNAME
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.kafka.saslSecretName }}
                  key: username
            - name: DFE_KAFKA_SASL_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.kafka.saslSecretName }}
                  key: password
            {{- end }}
            {{- end }}
            # Whether the stage pools sit behind a listener here, and where that
            # listener lives. The engine compiles a sender's destination, so it
            # has to address the pool the same way the charts front it -- the
            # shape of the address is apps.yaml's `mesh.host_pattern`, and these
            # two are the deployment's half of it. Absent means the pools are
            # dialled directly, which is every profile but mesh.
            - name: DFE_MESH_ENABLED
              value: {{ ternary "true" "false" (eq (include "dfe-common.meshEnabled" .) "true") | quote }}
            {{- if (include "dfe-common.meshEnabled" .) }}
            - name: DFE_MESH_NAMESPACE
              value: {{ .Values.mesh.gateway.namespace | quote }}
            {{- end }}
            {{- with .Values.stackVersion }}
            # The certified stack this deployment runs, from the cluster secret's
            # stack_version annotation the appset reads. GET
            # /api/v1/system/version prefers the deploy repo's pins.yaml and
            # falls back to this, so a deploy whose pins carry no base still
            # answers with a stack rather than nothing.
            - name: DFE_STACK_VERSION
              value: {{ . | quote }}
            {{- end }}
            {{- with .Values.profile }}
            # The tier this deployment was stood up as, from the SAME cluster
            # secret annotation that selects profile-<x>.yaml above. Without it
            # the console lists every optional app, because an engine that does
            # not know its tier cannot rule any of them out.
            - name: DFE_PROFILE
              value: {{ . | quote }}
            {{- end }}
            {{- with .Values.uiVersion }}
            # The dfe-ui pin this deployment renders, so the engine can report it
            # when the deploy repo's pins name no ui override.
            - name: DFE_UI_VERSION
              value: {{ . | quote }}
            {{- end }}
            # Reconcile the CH tenant fence on startup: tier roles, the _org_id row
            # policies, the deny policies over tables without one, and a CH user per
            # org/group. Set false only for a deployment with no tenant isolation.
            - name: DFE_TENANT_ISOLATION_ENABLED
              value: {{ .Values.tenantIsolation.enabled | quote }}
            # The engine settings loader reads DFE_CLICKHOUSE_* (or legacy
            # CLICKHOUSE_*), NOT a DFE_ENGINE_ prefix -- the old DFE_ENGINE_* names
            # were silently ignored, leaving CH at the localhost:9000 default. The
            # connection authenticates against the always-present `default` db;
            # the startup bootstrap CREATEs the DFE data database (clickhouse.database).
            # The engine has NO PostgreSQL (YAML/git SSoT) -- the old
            # DFE_ENGINE_PG_* env was dead.
            {{- include "dfe-engine.clickhouseEnv" . | nindent 12 }}
            # Default TTL for every time-series table; 0 = none; a source or a
            # dfe-schemas TTL overrides it.
            - name: DFE_CLICKHOUSE_DEFAULT_TTL_DAYS
              value: {{ .Values.retention.defaultTtlDays | quote }}
            {{- include "dfe-engine.clickhouseAuthEnv" . | nindent 12 }}
            {{- if .Values.huntRunner.enabled }}
            # The password the engine gives dfe_hunt_runner, from the Secret the
            # runner connects with, so the two pods never hold different values.
            - name: DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.huntRunner.clickhouse.passwordSecretName }}
                  key: {{ .Values.huntRunner.clickhouse.passwordSecretKey }}
            {{- end }}
            # Config-registry directories. WITHOUT these the engine leaves every
            # registry uninitialised and the config API returns 503 not_configured.
            # All live under the (writable) config mount; the engine seeds defaults
            # + does git-native CRUD here.
            - name: DFE_SOURCES_DIR
              value: {{ .Values.config.mountPath }}/sources
            - name: DFE_SERVICES_CONFIG_YAML_DIR
              value: {{ .Values.config.mountPath }}/services
            # Points the management API at the mounted manifest, so a new or
            # changed app reflects without an engine release.
            - name: DFE_APP_CATALOGUE_FILE
              value: /etc/dfe-engine/catalogue/apps.yaml
            # Unconditional because an absent directory is a no-op in the
            # engine, so a content entry added later needs no env change.
            - name: DFE_LIBRARY_SEED_DIR
              value: {{ .Values.content.mountPath }}/library
            # Where the settings API reads each app's config schema and
            # capability catalogue. Unconditional for the same reason: a
            # directory with no <app> subdirectory answers "no contract"
            # rather than failing, so an entry added later needs no env change.
            - name: DFE_APP_CONTRACT_DIR
              value: {{ .Values.content.mountPath }}/contract
            {{- with (include "dfe-engine.catalogueFile" .) }}
            # Set only where a content entry materialises one: a path naming a
            # file nothing wrote is refused rather than answered with no sources.
            - name: DFE_SOURCE_CATALOGUE_FILE
              value: {{ . | quote }}
            {{- end }}
            - name: DFE_SCHEMAS_DIR
              value: {{ .Values.config.mountPath }}/schemas
            - name: DFE_FIELDMAPS_DIR
              value: {{ .Values.config.mountPath }}/fieldmaps
            - name: DFE_HUNTS_DIR
              value: {{ .Values.config.mountPath }}/hunts
            - name: DFE_HUNTS_RULES_DIR
              value: {{ .Values.config.mountPath }}/rules
            - name: DFE_HUNTS_ALERT_DESTINATIONS_DIR
              value: {{ .Values.config.mountPath }}/alerts
            # The shared signing key. Supplying it means the engine adopts the
            # same key its sidecar pods read, instead of minting a private one
            # only it can see.
            - name: DFE_API_JWT_SECRET
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.auth.jwtSecretName }}
                  key: {{ .Values.auth.jwtSecretKey }}
            {{- with (.Values.api.forwardedAllowIps | default (dig "podCIDR" "" (.Values.networkModel | default dict))) }}
            # The proxy hops whose X-Forwarded-Proto and X-Forwarded-For are
            # believed, defaulting to the pod range the gateway and dfe-ui run in.
            # Unset, OIDC callbacks go out as http and every request audits as the
            # gateway's address.
            - name: DFE_API_FORWARDED_ALLOW_IPS
              value: {{ . | quote }}
            {{- end }}
            {{- with .Values.api.jwtExpireMinutes }}
            # How long an issued token stays valid. Unset leaves the engine's own
            # 60-minute default (settings.py: api.jwt_expire_minutes).
            - name: DFE_API_JWT_EXPIRE_MINUTES
              value: {{ . | quote }}
            {{- end }}
            {{- with (include "dfe-engine.optionalCount" (dict "key" "api.maxSessionMinutes" "value" .Values.api.maxSessionMinutes "min" 1)) }}
            # A refresh never extends a token past this many minutes from its
            # sign-in. Unset leaves the engine's own 720.
            - name: DFE_API_MAX_SESSION_MINUTES
              value: {{ . | quote }}
            {{- end }}
            {{- with (include "dfe-engine.docsEnabled" .) }}
            # Unset serves /docs and /redoc on a dev posture only; the gateway
            # and the links page read the same key.
            - name: DFE_API_DOCS_ENABLED
              value: {{ . | quote }}
            {{- end }}
            {{- with .Values.auth.proxyProvider }}
            # The provider a gateway login is stamped with, and so the only one
            # whose group links its asserted groups reach. Unset leaves `oidc`.
            - name: DFE_AUTH_PROXY_PROVIDER
              value: {{ . | quote }}
            {{- end }}
            {{- with (include "dfe-engine.optionalCount" (dict "key" "auth.apiKeyDefaultTtlDays" "value" .Values.auth.apiKeyDefaultTtlDays "min" 0)) }}
            # 0 is a key with no expiry, so the test is empty, not falsy.
            - name: DFE_AUTH_API_KEY_DEFAULT_TTL_DAYS
              value: {{ . | quote }}
            {{- end }}
            {{- with .Values.auth.loginThrottle }}
            {{- with (include "dfe-engine.optionalBool" (dict "key" "auth.loginThrottle.enabled" "value" .enabled)) }}
            - name: DFE_AUTH_LOGIN_THROTTLE_ENABLED
              value: {{ . | quote }}
            {{- end }}
            {{- with (include "dfe-engine.optionalCount" (dict "key" "auth.loginThrottle.usernameFailures" "value" .usernameFailures "min" 1)) }}
            - name: DFE_AUTH_LOGIN_THROTTLE_USERNAME_FAILURES
              value: {{ . | quote }}
            {{- end }}
            {{- with (include "dfe-engine.optionalCount" (dict "key" "auth.loginThrottle.clientFailures" "value" .clientFailures "min" 1)) }}
            - name: DFE_AUTH_LOGIN_THROTTLE_CLIENT_FAILURES
              value: {{ . | quote }}
            {{- end }}
            {{- with (include "dfe-engine.optionalCount" (dict "key" "auth.loginThrottle.maxDelaySeconds" "value" .maxDelaySeconds "min" 1)) }}
            - name: DFE_AUTH_LOGIN_THROTTLE_MAX_DELAY_SECONDS
              value: {{ . | quote }}
            {{- end }}
            {{- end }}
            # Secrets the engine MINTS (JWT signing key, per-group CH pw) go under
            # the WRITABLE config mount. The default ./.secrets resolves to
            # /app/.secrets on the root-owned WORKDIR (non-writable) -> the engine
            # crashes minting the JWT signing key at startup. (Same fix as dfe-docker.)
            - name: DFE_SECRETS_PATH
              value: {{ .Values.config.mountPath }}/.secrets
            # Auth stores (accounts/groups/api-keys, + oidc-providers) -- same
            # trap as DFE_SECRETS_PATH: without this the auth bootstrap falls
            # back to ./config/auth under the read-only /app WORKDIR and the
            # pod crashloops at startup (v1.10.7+, dfe-engine#106). emptyDir =
            # per-pod ephemeral: fine at 1 replica; >1 replica needs shared
            # storage for cross-pod JWKS (image-side work, dfe-engine#106).
            - name: DFE_AUTH_DIR
              value: {{ .Values.config.mountPath }}/auth
            # The CLASS-WIDE guard for the same fallback: auth is not the only
            # startup mkdir that defaults under cwd -- the org and
            # service-surface registries (and rbac/connections.yaml lookup)
            # resolve config/<x> under /app when DFE_CONFIG_DIR is unset, so
            # fixing auth alone just moves the crash to the next registry.
            # Point the shared root at the writable config mount.
            - name: DFE_CONFIG_DIR
              value: {{ .Values.config.mountPath }}
            {{- if gt (len .Values.seedAuth.seedAccounts) 0 }}
            # Stable named team logins, reconciled on every boot -- config wins,
            # so a teardown+rebuild restores the exact shared logins (#106).
            # Only referenced when the overlay supplies accounts, so an absent
            # Secret never wedges the pod in CreateContainerConfigError (same
            # guard idiom as the hyperdx chart's passwordSecretName).
            - name: DFE_AUTH_LOCAL_SEED_ACCOUNTS
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.seedAuth.secretName }}
                  key: seed-accounts
            {{- end }}
            {{- if .Values.auth.adminSecretName }}
            # The MINTED admin password (#233). The engine reasserts it on every
            # boot and refuses to start on an unset or `changeme` value unless
            # env is a dev posture. An empty adminSecretName is the dev-only
            # no-Secret case.
            - name: DFE_AUTH_LOCAL_ADMIN_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.auth.adminSecretName }}
                  key: {{ .Values.auth.adminSecretKey }}
            # The engine composes the login page's credential-fetch command from
            # these and the namespace below.
            - name: DFE_AUTH_LOCAL_ADMIN_SECRET_NAME
              value: {{ .Values.auth.adminSecretName | quote }}
            - name: DFE_AUTH_LOCAL_ADMIN_SECRET_KEY
              value: {{ .Values.auth.adminSecretKey | quote }}
            {{- end }}
            - name: DFE_DEPLOYMENT_NAMESPACE
              valueFrom:
                fieldRef:
                  fieldPath: metadata.namespace
            {{- with .Values.auth.adminSecretPath }}
            # Set = the rotate-password endpoint writes through the scalo secrets
            # seam instead of answering 501 with the kubectl patch command.
            - name: DFE_AUTH_LOCAL_ADMIN_PASSWORD_SECRET_PATH
              value: {{ . | quote }}
            {{- end }}
            {{- if .Values.auth.breakglassSecretName }}
            # First-boot break-glass password: the engine hashes it into the
            # deploy repo once and ignores it afterwards.
            - name: DFE_AUTH_BREAKGLASS_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.auth.breakglassSecretName }}
                  key: {{ .Values.auth.breakglassSecretKey }}
            {{- end }}
            {{- with (include "dfe-common.otelEndpoint" .) }}
            - name: OTEL_EXPORTER_OTLP_ENDPOINT   # self-monitoring; telemetry.mode-resolved
              value: {{ . | quote }}
            {{- end }}
            - name: OTEL_SERVICE_NAME
              value: dfe-engine
          {{- if .Values.gitops.enabled }}
            - name: DFE_GITOPS_ENABLED
              value: "true"
            - name: DFE_GITOPS_REPO_URL
              value: {{ .Values.gitops.repoUrl | quote }}
            - name: DFE_GITOPS_BRANCH
              value: {{ .Values.gitops.branch | quote }}
            - name: DFE_GITOPS_LOCAL_PATH
              value: {{ .Values.gitops.localPath | quote }}
            - name: DFE_GITOPS_USERNAME
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.gitops.credentialsSecret }}
                  key: username
            - name: DFE_GITOPS_TOKEN
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.gitops.credentialsSecret }}
                  key: password
          {{- end }}
          {{- if .Values.hyperdx.enabled }}
            # Machine-JWT control surface: the engine confirms the fork's
            # default team and seeds connections (settings.hyperdx.*).
            - name: DFE_HYPERDX_ENABLED
              value: "true"
            - name: DFE_HYPERDX_BASE_URL
              value: {{ include "dfe-engine.hyperdxBaseUrl" . | quote }}
          {{- end }}
          {{- if .Values.auth.trustProxyHeaders }}
            # The engine believes X-Oidc-* only where a gateway both STRIPS the
            # inbound values and injects verified ones. Its own default is false,
            # and this must stay a deliberate deployment statement -- see the
            # value's comment for what has to be true before setting it.
            - name: DFE_AUTH_TRUST_PROXY_AUTH_HEADERS
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
            # Private-CA IdPs and datastores verify against this bundle. It is the
            # init container's MERGE of the configured CA and the image's public
            # roots, not the ConfigMap itself -- see the seed script.
            - name: SSL_CERT_FILE
              value: {{ .Values.config.mountPath }}/auth/ca-bundle.pem
          {{- end }}
{{- end -}}
