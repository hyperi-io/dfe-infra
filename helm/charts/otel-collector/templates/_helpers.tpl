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
