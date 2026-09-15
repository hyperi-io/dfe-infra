{{/*
kafbat.securityProtocol -- derive security.protocol from a provider key.
Mirrors the canonical provider table (scalo-rs transport/kafka/providers.rs,
scalo-py scalo.kafka.providers) -- the DFE Kafka credential contract
(dfe-engine#98). Kept in sync by hand: if the canonical table changes, update
here too.
*/}}
{{- define "kafbat.securityProtocol" -}}
{{- $provider := . -}}
{{- if eq $provider "plaintext" -}}
PLAINTEXT
{{- else if or (eq $provider "strimzi") (eq $provider "redpanda") (eq $provider "msk") (eq $provider "redpanda-cloud") (eq $provider "confluent-cloud") (eq $provider "msk_iam") -}}
SASL_SSL
{{- else -}}
{{- fail (printf "kafbat: unknown kafka provider %q; expected one of: strimzi, redpanda, msk, redpanda-cloud, confluent-cloud, plaintext, msk_iam" $provider) -}}
{{- end -}}
{{- end }}

{{/*
kafbat.saslMechanism -- derive sasl.mechanism from a provider key. Empty
string for plaintext (no SASL). NEVER hand-set by an operator -- this is the
single derivation point, matching scalo's KafkaConfig.provider behaviour.
*/}}
{{- define "kafbat.saslMechanism" -}}
{{- $provider := . -}}
{{- if eq $provider "confluent-cloud" -}}
PLAIN
{{- else if eq $provider "msk_iam" -}}
OAUTHBEARER
{{- else if eq $provider "plaintext" -}}
{{- else if or (eq $provider "strimzi") (eq $provider "redpanda") (eq $provider "msk") (eq $provider "redpanda-cloud") -}}
SCRAM-SHA-512
{{- else -}}
{{- fail (printf "kafbat: unknown kafka provider %q; expected one of: strimzi, redpanda, msk, redpanda-cloud, confluent-cloud, plaintext, msk_iam" $provider) -}}
{{- end -}}
{{- end }}

{{/*
kafbat.jaasModule -- pick the JAAS login module class for a sasl mechanism.
Mirrors scalo-py's toolconfig._jaas_config(): SCRAM* -> ScramLoginModule,
PLAIN -> PlainLoginModule. OAUTHBEARER (msk_iam) has no JAAS module -- it
needs its own callback handler, so this fails loudly rather than emit a
broken config (same behaviour as the Python emitter).
*/}}
{{- define "kafbat.jaasModule" -}}
{{- $mechanism := . -}}
{{- if hasPrefix "SCRAM" $mechanism -}}
org.apache.kafka.common.security.scram.ScramLoginModule
{{- else if eq $mechanism "PLAIN" -}}
org.apache.kafka.common.security.plain.PlainLoginModule
{{- else -}}
{{- fail (printf "kafbat: no JAAS module known for sasl mechanism %q (IAM-family mechanisms need their own callback handler, not JAAS -- msk_iam cannot be wired via this chart's clusters list today)" $mechanism) -}}
{{- end -}}
{{- end }}

{{/*
kafbat.hasSchemaRegistry -- true when the provider ships a bundled schema
registry (Confluent Cloud, Redpanda self-hosted + Cloud). Mirrors
KnownProvider::schema_registry() in scalo-rs providers.rs. Renders "true" or
"" so callers can use it with `if`.
*/}}
{{- define "kafbat.hasSchemaRegistry" -}}
{{- $provider := . -}}
{{- if or (eq $provider "confluent-cloud") (eq $provider "redpanda") (eq $provider "redpanda-cloud") -}}
true
{{- end -}}
{{- end }}

{{/*
kafbat.clusterEnvPrefix -- the env var prefix for a cluster's credentials,
by index: KAFKA_CLUSTER_<i>_USERNAME / KAFKA_CLUSTER_<i>_PASSWORD. Referenced
from the mounted application-local.yml as a Spring ${VAR} placeholder (same
pattern as OAUTH_CLIENT_SECRET below) so plaintext credentials never land in
the ConfigMap.
*/}}
{{- define "kafbat.clusterEnvPrefix" -}}
{{- printf "KAFKA_CLUSTER_%d" . -}}
{{- end }}

{{/*
kafbat.effectiveAuthType -- the auth type actually rendered. OAUTH2 is only
honoured when the oidc block is FILLED, which takes more than oidc.enabled:
the issuer, the client id and the client secret's name are all required.

enabled alone is not enough, and the gap is not cosmetic. The cloud values
overlays set oidc.enabled for the whole deployment, so a deployment with no
kafbat provider yet rendered OAUTH_CLIENT_SECRET with an empty secretKeyRef
name, which the API server REJECTS -- the Deployment never applied and the
Application could not sync at all.

Without a filled block the chart degrades to LOGIN_FORM (break-glass) so a
vanilla deployment with an empty overlay still BOOTS -- kafbat exits 1
("OAuth2 authentication is enabled but no providers specified") if
auth.type=OAUTH2 reaches it bare. OIDC takes over the moment the overlay
fills oidc.*. DISABLED passes through.
*/}}
{{- define "kafbat.oidcIsUsable" -}}
{{- if and .Values.oidc.enabled .Values.oidc.issuerUri .Values.oidc.clientId .Values.oidc.clientSecretName -}}
true
{{- end -}}
{{- end }}

{{- define "kafbat.effectiveAuthType" -}}
{{- if and (eq .Values.auth.type "OAUTH2") (not (include "kafbat.oidcIsUsable" .)) -}}
LOGIN_FORM
{{- else -}}
{{- .Values.auth.type -}}
{{- end -}}
{{- end }}
