{{/*
dfe-common.replicas — emit `replicas:` ONLY when KEDA is not scaling this app.

Once a ScaledObject exists its HPA owns the replica count, so a chart that also
renders `replicas:` gives Argo selfHeal a rendered value to revert every scale
back to. Kubernetes defaults an absent `replicas` to 1 and then leaves it alone,
which is the shape KEDA documents. `ignoreDifferences` on /spec/replicas in the
Argo Application covers charts this library does not own.

Usage (templates/deployment.yaml):
  spec:
    {{- include "dfe-common.replicas" . }}
    selector:
*/}}
{{- define "dfe-common.replicas" -}}
{{- if not (and .Values.keda .Values.keda.enabled) }}
  replicas: {{ .Values.replicaCount | default 1 }}
{{- end }}
{{- end -}}
