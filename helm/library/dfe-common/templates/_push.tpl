{{/*
dfe-common.pushService -- the scalo Push listener's Service, for a stage that
RECEIVES records on the direct transport.

On the bus a stage is addressed by topic and needs no Service at all, so this
renders only where dfe-common.transport says direct. Port 6000 named `push` is
the platform convention apps.yaml records under `endpoints`, and the engine
builds a stage's endpoint from the Service name and that port -- so a chart that
moves its listener is a manifest edit, never an engine release.

dfe-loader has its own Service (named port `grpc`, rendered on both transports)
because the receiver has dialled it since before this helper existed; renaming a
live Service port buys nothing.

Usage (templates/service.yaml, the whole file):
  {{- include "dfe-common.pushService" . }}
*/}}
{{- define "dfe-common.pushService" -}}
{{- if eq (include "dfe-common.transport" .) "direct" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "dfe-common.fullname" . }}
  labels:
    {{- include "dfe-common.labels" . | nindent 4 }}
spec:
  # ClusterIP only: a pipeline stage is east-west and never faces a client.
  type: ClusterIP
  ports:
    - port: {{ include "dfe-common.pushPort" . }}
      targetPort: push
      protocol: TCP
      name: push
      # Cleartext HTTP/2, the protocol gRPC runs on. A proxy in front of the
      # pool reads this to decide how to speak to it, and without it Envoy
      # Gateway dials the pool as HTTP/1.1 and every request resets.
      appProtocol: kubernetes.io/h2c
  selector:
    {{- include "dfe-common.selectorLabels" . | nindent 4 }}
{{- end -}}
{{- end -}}

{{/*
dfe-common.pushContainerPort -- the matching container port, so the Service's
targetPort resolves. Same gate, so the two can never disagree.

Usage (container ports list):
  ports:
    {{- include "dfe-common.pushContainerPort" . | nindent 12 }}
*/}}
{{- define "dfe-common.pushContainerPort" -}}
{{- if eq (include "dfe-common.transport" .) "direct" -}}
- name: push
  containerPort: {{ include "dfe-common.pushPort" . }}
  protocol: TCP
{{- end -}}
{{- end -}}

{{/*
dfe-common.pushPort -- the listener port, chart-overridable via service.pushPort.
*/}}
{{- define "dfe-common.pushPort" -}}
{{- if .Values.service -}}
{{- default 6000 .Values.service.pushPort -}}
{{- else -}}
6000
{{- end -}}
{{- end -}}
