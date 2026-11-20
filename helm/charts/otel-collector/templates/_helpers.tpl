{{/*
otel-collector.daemonsetContainerName -- the daemonset's container name. The
filelog exclude in configmap.yaml is built from it, because kubelet builds the
pod log path from the container name.
*/}}
{{- define "otel-collector.daemonsetContainerName" -}}
otel-collector
{{- end -}}

{{/*
otel-collector.ingressTokenDir -- where the ingress token Secret is mounted. The
extension's filename and the Deployment's mount both derive from it.
*/}}
{{- define "otel-collector.ingressTokenDir" -}}
/etc/otel-ingress
{{- end -}}

{{/*
otel-collector.clickhouseInClusterTLS -- "true" when the in-cluster ClickHouse
serves its native protocol over TLS: clickhouse.tls.enabled and a CA to sign with
(an issuerRef, or the internal CA), the rule every client applies to the shared
block. An external server stays on its own settings.
*/}}
{{- define "otel-collector.clickhouseInClusterTLS" -}}
{{- $ch := .Values.clickhouse | default dict -}}
{{- $tls := dig "tls" dict $ch -}}
{{- $issuer := or (dig "issuerRef" "name" "" $tls) (eq (toString (dig "internalCA" "present" false $tls)) "true") -}}
{{- if and (ne (toString (dig "mode" "" $ch)) "external") (eq (toString (dig "enabled" false $tls)) "true") $issuer -}}true{{- end -}}
{{- end -}}

{{/*
otel-collector.clickhouseNativePort -- the native port the collector dials: the
TLS port when the in-cluster server serves it, else clickhouse.nativePort.
*/}}
{{- define "otel-collector.clickhouseNativePort" -}}
{{- if include "otel-collector.clickhouseInClusterTLS" . -}}
{{- dig "tls" "nativePort" 9440 (.Values.clickhouse | default dict) -}}
{{- else -}}
{{- .Values.clickhouse.nativePort -}}
{{- end -}}
{{- end -}}

{{/*
otel-collector.clickhouseSecure -- "true" when the collector dials ClickHouse over
TLS: an in-cluster server serving it, or an external one clickhouse.tls.enabled
says speaks it.
*/}}
{{- define "otel-collector.clickhouseSecure" -}}
{{- $ch := .Values.clickhouse | default dict -}}
{{- $external := eq (toString (dig "mode" "" $ch)) "external" -}}
{{- if or (include "otel-collector.clickhouseInClusterTLS" .) (and $external (eq (toString (dig "tls" "enabled" false $ch)) "true")) -}}true{{- end -}}
{{- end -}}

{{/*
otel-collector.clickhouseVerify -- "" when clickhouse.tls.verify is false, for a
server whose certificate cannot be verified; "true" otherwise.
*/}}
{{- define "otel-collector.clickhouseVerify" -}}
{{- if ne (toString (dig "tls" "verify" true (.Values.clickhouse | default dict))) "false" -}}true{{- end -}}
{{- end -}}

{{/*
otel-collector.clickhouseQuery -- the query string the exporter and the sql_query
receiver append to the native address: secure=true when the server speaks TLS
(skip_verify=true as well when verification is off).
*/}}
{{- define "otel-collector.clickhouseQuery" -}}
{{- if include "otel-collector.clickhouseSecure" . -}}
?secure=true{{ if not (include "otel-collector.clickhouseVerify" .) }}&skip_verify=true{{ end }}
{{- end -}}
{{- end -}}

{{/*
otel-collector.clickhouseCADir -- where the CA that signed ClickHouse's certificate
is mounted. SSL_CERT_DIR points at it, because the sql_query driver reads the
process's trust store.
*/}}
{{- define "otel-collector.clickhouseCADir" -}}
/etc/dfe-clickhouse-ca
{{- end -}}

{{/*
otel-collector.clickhouseCAKind -- "secret" when the in-cluster server's CA copy
is mounted, "configmap" when the deployment names a CA bundle of its own for an
external server, "" when the system roots are enough.
*/}}
{{- define "otel-collector.clickhouseCAKind" -}}
{{- $ca := dig "tls" "ca" dict (.Values.clickhouse | default dict) -}}
{{- if include "otel-collector.clickhouseVerify" . -}}
{{- if and (include "otel-collector.clickhouseInClusterTLS" .) $ca.secretName -}}secret
{{- else if and (include "otel-collector.clickhouseSecure" .) $ca.configMapName -}}configmap
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
otel-collector.clickhouseUser / clickhouseSecret -- the collector's own
ClickHouse user and the Secret its password is in: exporters.clickhouse when
set, else clickhouse.serviceUsers.otelWriter. The admin account is refused.
*/}}
{{- define "otel-collector.clickhouseUser" -}}
{{- $user := .Values.exporters.clickhouse.username | default (dig "serviceUsers" "otelWriter" "user" "" .Values.clickhouse) -}}
{{- if not $user -}}
{{- fail "the collector has no ClickHouse user: set clickhouse.serviceUsers.otelWriter.user" -}}
{{- end -}}
{{- if eq $user (toString (dig "user" "" .Values.clickhouse)) -}}
{{- fail (printf "the collector's ClickHouse user is %q, the admin account dfe-engine alone holds -- it writes as clickhouse.serviceUsers.otelWriter" $user) -}}
{{- end -}}
{{- $user -}}
{{- end -}}

{{- define "otel-collector.clickhouseSecret" -}}
{{- $secret := .Values.exporters.clickhouse.passwordSecretName | default (dig "serviceUsers" "otelWriter" "passwordSecretName" "" .Values.clickhouse) -}}
{{- if not $secret -}}
{{- fail "the collector has no ClickHouse password Secret: set clickhouse.serviceUsers.otelWriter.passwordSecretName" -}}
{{- end -}}
{{- if eq $secret (toString (dig "passwordSecretName" "" .Values.clickhouse)) -}}
{{- fail (printf "the collector's ClickHouse password is %q, the admin Secret dfe-engine alone reads -- it writes as clickhouse.serviceUsers.otelWriter" $secret) -}}
{{- end -}}
{{- $secret -}}
{{- end -}}

{{/*
otel-collector.waitForEngine -- dfe-common.waitForEngine's init container, on
busybox's wget. Rendered only while the gateway connects to ClickHouse, and
test_clickhouse_app_identity.py holds the script to the library's. See
docs/clickhouse-identity.md.
*/}}
{{- define "otel-collector.waitForEngine" -}}
{{- $w := .Values.waitForEngine | default dict -}}
{{- $enabled := true -}}
{{- if hasKey $w "enabled" -}}{{- $enabled = $w.enabled -}}{{- end -}}
{{- if and $enabled (or .Values.exporters.clickhouse.enabled .Values.clusterTelemetry.clickhouse.enabled) }}
{{- $service := $w.service | default "dfe-engine" -}}
{{- $namespace := $w.namespace | default .Values.appNamespace | default (include "dfe-common.namespace" .) -}}
{{- $port := $w.port | default 8000 -}}
{{- $timeout := $w.timeoutSeconds | default 600 -}}
{{- $interval := $w.intervalSeconds | default 5 -}}
{{- $image := $w.image | default dict -}}
{{- if not (and $image.repository $image.tag $image.digest) -}}
{{- fail "waitForEngine.image needs a repository, tag and digest: versions.yaml services.busybox and services-digests.busybox" -}}
{{- end }}
initContainers:
  - name: wait-for-engine
    image: {{ printf "%s:%s@%s" $image.repository $image.tag $image.digest | quote }}
    imagePullPolicy: {{ $image.pullPolicy | default "IfNotPresent" }}
    {{- include "dfe-common.containerSecurityContext" . | nindent 4 }}
    {{- with $w.resources }}
    resources:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    command:
      - /bin/sh
      - -c
      - |
        set -eu
        URL="http://{{ $service }}.{{ $namespace }}.svc.cluster.local:{{ $port }}/readyz"
        DEADLINE=$(( $(date +%s) + {{ $timeout }} ))
        echo "waiting for ${URL}"
        until wget -q -O /dev/null -T 5 "$URL"; do
          if [ "$(date +%s)" -ge "$DEADLINE" ]; then
            echo "dfe-engine did not report ready at ${URL} within {{ $timeout }}s -- GET /api/v1/system/schema on the engine says why" >&2
            exit 1
          fi
          sleep {{ $interval }}
        done
        echo "dfe-engine is ready"
{{- end }}
{{- end -}}

{{/*
otel-collector.validateIngress -- the otel.ingress render guards. The messages
match the gateway chart's (helm/edge/gateway _routes.tpl) word for word.
*/}}
{{- define "otel-collector.validateIngress" -}}
{{- $ingress := .Values.otel.ingress -}}
{{- if not (kindIs "bool" $ingress.enabled) -}}
{{- fail (printf "otel.ingress.enabled is %v (a %s), not a bool -- a quoted \"false\" is a non-empty string and truthy, so it would publish OTLP ingest. Write true or false unquoted" $ingress.enabled (kindOf $ingress.enabled)) -}}
{{- end -}}
{{- if and $ingress.enabled (not $ingress.auth.remoteKey) -}}
{{- fail "otel.ingress.enabled is true and otel.ingress.auth.remoteKey is empty -- OTLP ingest from outside the cluster admits only a bearer token, and this key names where the deployment's secret store holds it. Store the token (property token) at a path such as <project>/<env>/otel/ingress and set otel.ingress.auth.remoteKey to it in the deploy repo's infra/common.yaml. There is no unauthenticated mode" -}}
{{- end -}}
{{- end -}}

{{/*
otel-collector.validatePriorityClass -- the API server refuses a created class
named system-* or valued above 1,000,000,000.
*/}}
{{- define "otel-collector.validatePriorityClass" -}}
{{- $pc := .Values.daemonset.priorityClass -}}
{{- if $pc.create -}}
{{- if not $pc.name -}}
{{- fail "daemonset.priorityClass.create is true and daemonset.priorityClass.name is empty -- name the class to create, or set create false" -}}
{{- end -}}
{{- if hasPrefix "system-" (toString $pc.name) -}}
{{- fail (printf "daemonset.priorityClass.name is %q -- the system- prefix is reserved for the cluster's own classes and the API server refuses to create one. Name a class of your own, or set daemonset.priorityClass.create false to use an existing one" (toString $pc.name)) -}}
{{- end -}}
{{- if or (le (int64 $pc.value) 0) (gt (int64 $pc.value) 1000000000) -}}
{{- fail (printf "daemonset.priorityClass.value is %v -- above 0 to outrank a pod that names no class, and at most 1000000000, the ceiling on a user-defined class" $pc.value) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
otel-collector.kafkaProvider -- the managed broker kafka.external.provider names,
or "" for a broker the stack deploys. Every managed provider speaks SASL_SSL.
*/}}
{{- define "otel-collector.kafkaProvider" -}}
{{- if eq (toString .Values.kafka.mode) "external" -}}
{{- dig "provider" "" (.Values.kafka.external | default dict) -}}
{{- end -}}
{{- end }}

{{/*
otel-collector.kafkaMechanism -- the SASL mechanism the kafka_metrics receiver
dials with: the explicit value, else the one the provider table derives for the
managed broker, else SCRAM-SHA-512 (the shipped broker's).
*/}}
{{- define "otel-collector.kafkaMechanism" -}}
{{- $provider := include "otel-collector.kafkaProvider" . -}}
{{- if .Values.clusterTelemetry.kafka.sasl.mechanism -}}
{{- .Values.clusterTelemetry.kafka.sasl.mechanism -}}
{{- else if $provider -}}
{{- include "dfe-common.kafkaSaslMechanism" $provider -}}
{{- else -}}
SCRAM-SHA-512
{{- end -}}
{{- end }}

{{/*
otel-collector.kafkaTLS -- "true" when kafka_metrics must dial TLS: asked for
explicitly, or the broker is a managed provider. Renders "" otherwise.
*/}}
{{- define "otel-collector.kafkaTLS" -}}
{{- if or .Values.clusterTelemetry.kafka.tls.enabled (include "otel-collector.kafkaProvider" .) -}}
true
{{- end -}}
{{- end }}

{{/*
otel-collector.validateProviderLogs -- the providerLogs render guards. A flavour
that names a source it cannot read fails the render.
*/}}
{{- define "otel-collector.validateProviderLogs" -}}
{{- $pl := .Values.clusterTelemetry.providerLogs -}}
{{- if not (has $pl.flavour (list "none" "aws" "azure" "gcp" "rancher")) -}}
{{- fail (printf "clusterTelemetry.providerLogs.flavour is %q -- one of none, aws, azure, gcp, rancher" (toString $pl.flavour)) -}}
{{- end -}}
{{- if eq $pl.flavour "aws" -}}
{{- if not $pl.aws.region -}}
{{- fail "clusterTelemetry.providerLogs.flavour is aws and aws.region is empty -- the receiver cannot pick a region for itself" -}}
{{- end -}}
{{- if not (or $pl.aws.eksAuditLogGroup $pl.aws.cloudTrailLogGroup) -}}
{{- fail "clusterTelemetry.providerLogs.flavour is aws and no log group is named -- set aws.eksAuditLogGroup and/or aws.cloudTrailLogGroup (the cloud root outputs both)" -}}
{{- end -}}
{{- end -}}
{{- if and (eq $pl.flavour "azure") (not $pl.azure.connectionSecretName) -}}
{{- fail "clusterTelemetry.providerLogs.flavour is azure and azure.connectionSecretName is empty -- name the Secret holding the Event Hub connection string" -}}
{{- end -}}
{{- if and (eq $pl.flavour "gcp") (not $pl.gcp.subscription) -}}
{{- fail "clusterTelemetry.providerLogs.flavour is gcp and gcp.subscription is empty -- name the Pub/Sub subscription as projects/<project>/subscriptions/<name>" -}}
{{- end -}}
{{- if and (eq $pl.flavour "rancher") (not $pl.rancher.auditLogPath) -}}
{{- fail "clusterTelemetry.providerLogs.flavour is rancher and rancher.auditLogPath is empty -- name the kube-apiserver audit log file" -}}
{{- end -}}
{{- end -}}

{{/* The provider-logs workloads' ServiceAccount, separate so a cloud role bound to it reaches nothing else. */}}
{{- define "otel-collector.providerLogsServiceAccount" -}}
{{- printf "%s-provider-logs" (include "dfe-common.fullname" .) -}}
{{- end -}}
