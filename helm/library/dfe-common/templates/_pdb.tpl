{{/*
dfe-common.pdb -- PodDisruptionBudget for the chart's primary workload.

Renders ONLY when BOTH hold:
  - pdb.enabled is not set to false (default ON), AND
  - the rendered replicaCount is >= 2.
The replica gate keeps single-replica profiles PDB-free, and the budget is
expressed as maxUnavailable (NOT minAvailable) because the gate reads the
STATIC replicaCount -- an autoscaler (KEDA) can later shrink the workload to
one pod, where `minAvailable: 1` would compute disruptionsAllowed=0 and block
every node drain until someone intervenes. `maxUnavailable: 1` protects a
multi-replica workload identically (one pod may be evicted at a time) while
still letting a scaled-to-one pod evict and reschedule.

Override via pdb.maxUnavailable; setting pdb.minAvailable switches to that
form EXPLICITLY -- only do it for workloads whose floor the autoscaler can
never take below minAvailable+1. Selector matches dfe-common.selectorLabels,
i.e. the chart's primary Deployment. Workloads with their own selector (e.g.
the otel gateway) need their own PDB manifest.

Usage (a chart opts in by shipping a templates/pdb.yaml containing):
  {{- include "dfe-common.pdb" . }}
*/}}
{{- define "dfe-common.pdb" -}}
{{- $enabled := true -}}
{{- $maxUnavailable := 1 -}}
{{- $minAvailable := 0 -}}
{{- with .Values.pdb -}}
{{- if hasKey . "enabled" }}{{- $enabled = .enabled -}}{{- end -}}
{{- $maxUnavailable = .maxUnavailable | default 1 -}}
{{- $minAvailable = .minAvailable | default 0 -}}
{{- end -}}
{{- if and $enabled (ge (int (.Values.replicaCount | default 1)) 2) }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "dfe-common.fullname" . }}
  labels:
    {{- include "dfe-common.labels" . | nindent 4 }}
spec:
  {{- if $minAvailable }}
  minAvailable: {{ $minAvailable }}
  {{- else }}
  maxUnavailable: {{ $maxUnavailable }}
  {{- end }}
  selector:
    matchLabels:
      {{- include "dfe-common.selectorLabels" . | nindent 6 }}
{{- end }}
{{- end }}
