{{/*
otel-collector.daemonsetContainerName -- the daemonset's own container name.

Kubelet builds the pod log path (/var/log/pods/<pod>/<container>/*.log) from
this name, so the filelog receiver's exclude pattern in configmap.yaml has to
match it exactly. Centralised here so the two cannot drift apart the way they
did when the exclude carried `otc-container`, the UPSTREAM chart's name --
the collector then shipped its own logs, one error line per failed export.
*/}}
{{- define "otel-collector.daemonsetContainerName" -}}
otel-collector
{{- end -}}

{{/*
otel-collector.ingressTokenDir -- where the ingress token Secret is mounted. The
bearertokenauth extension's filename and the Deployment's mount both derive from
it, so the file the extension reads is the one the Secret projects.
*/}}
{{- define "otel-collector.ingressTokenDir" -}}
/etc/otel-ingress
{{- end -}}

{{/*
otel-collector.clickhouseQuery -- the query string the exporter and the sqlquery
receiver append to the native address: secure=true on an external server that
speaks TLS. An in-cluster server stays on plaintext 9000 whatever clickhouse.tls
says, because clickhouse-cluster keeps 9000 open beside 9440 for this collector.
*/}}
{{- define "otel-collector.clickhouseQuery" -}}
{{- $ch := .Values.clickhouse | default dict -}}
{{- $external := eq (toString (dig "mode" "" $ch)) "external" -}}
{{- if and $external (eq (toString (dig "tls" "enabled" false $ch)) "true") -}}?secure=true{{- end -}}
{{- end -}}

{{/*
otel-collector.clickhouseUser / clickhouseSecret -- the collector's own
ClickHouse user and the Secret its password is in: exporters.clickhouse when
set, else clickhouse.serviceUsers.otelWriter. The exporter and the sqlquery
receiver both use them. The admin account is refused, because dfe-engine alone
holds it.
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
otel-collector.validateIngress -- the otel.ingress render guards. The messages
are the gateway chart's own, word for word (helm/edge/gateway _routes.tpl), so
both refusals read the same whichever chart an operator renders first.
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
named system-* or valued above 1,000,000,000, and it refuses the daemonset's
pods while the class they name does not exist.
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
