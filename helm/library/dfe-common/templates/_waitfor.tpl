{{/*
dfe-common.waitForEngine -- an init container that holds an app back until
dfe-engine reports ready.

dfe-engine applies every ClickHouse object and every bootstrap Kafka topic at its
own startup and gates /readyz on the result, so "the engine is ready" is "the
schema and the topics are there". An app that starts first finds no table and no
topic: dfe-loader holds every message pending-schema and dead-letters it, which
reads as data loss and is really start ordering.

Argo's wave order gates the SYNC, not the pod, and neither gates a Helm install
at all -- this is the per-pod half of the same guarantee.

It polls the engine Service, so it can only succeed once a READY engine pod is in
that Service's endpoints. Bounded: on expiry it exits non-zero, the kubelet
restarts it under the pod's restartPolicy and the pod sits in
Init:CrashLoopBackOff. The app container never starts against an absent schema,
and the pod converges on its own the moment the engine reports ready.

Reads .Values.waitForEngine (enabled, service, namespace, port, timeoutSeconds,
intervalSeconds, resources), each with a default here so a chart that declares
none still renders the working shape.

Usage:
  spec:
    {{- include "dfe-common.waitForEngine" . | nindent 6 }}
      containers:
*/}}
{{- define "dfe-common.waitForEngine" -}}
{{- $w := .Values.waitForEngine | default dict -}}
{{- $enabled := true -}}
{{- if hasKey $w "enabled" -}}{{- $enabled = $w.enabled -}}{{- end -}}
{{- if $enabled }}
{{- $service := $w.service | default "dfe-engine" -}}
{{- $namespace := $w.namespace | default .Release.Namespace -}}
{{- $port := $w.port | default 8000 -}}
{{- $timeout := $w.timeoutSeconds | default 600 -}}
{{- $interval := $w.intervalSeconds | default 5 -}}
initContainers:
  - name: wait-for-engine
    image: {{ include "dfe-common.image" . | quote }}
    imagePullPolicy: {{ .Values.image.pullPolicy }}
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
        until curl -fsS -o /dev/null --max-time 5 "$URL"; do
          if [ "$(date +%s)" -ge "$DEADLINE" ]; then
            echo "dfe-engine did not report ready at ${URL} within {{ $timeout }}s -- GET /api/v1/system/schema on the engine says why" >&2
            exit 1
          fi
          sleep {{ $interval }}
        done
        echo "dfe-engine is ready"
{{- end }}
{{- end }}
