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
