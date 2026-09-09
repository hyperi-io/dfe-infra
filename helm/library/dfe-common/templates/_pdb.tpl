{{/*
dfe-common.pdb -- PodDisruptionBudget for the chart's primary workload.

Renders ONLY when BOTH hold:
  - pdb.enabled is not set to false (default ON), AND
  - the workload can carry two pods: replicaCount >= 2, or KEDA is on with
    minReplicaCount >= 2.
The gate keeps single-replica profiles PDB-free, where any budget at all would
block every node drain.

The budget FORM follows the workload's FLOOR -- the smallest replica count
anything can take it to, which is KEDA's minReplicaCount where KEDA drives it
and replicaCount where it does not:

  floor >= 2  ->  minAvailable: 1. Nothing can shrink the workload to one pod,
                  so one pod may always be evicted and a drain never wedges.
  floor  = 1  ->  maxUnavailable: 1. The static replicaCount gate passed, but an
                  autoscaler can still take this to one pod, where minAvailable:
                  1 computes disruptionsAllowed=0 and blocks every drain until
                  someone intervenes. maxUnavailable protects a multi-replica
                  workload identically and still lets a scaled-to-one pod evict.

Override via pdb.maxUnavailable or pdb.minAvailable, which pins that form
explicitly. Selector matches dfe-common.selectorLabels, i.e. the chart's primary
Deployment. Workloads with their own selector (e.g. the otel gateway) need their
own PDB manifest.

Usage (a chart opts in by shipping a templates/pdb.yaml containing):
  {{- include "dfe-common.pdb" . }}
*/}}
{{- define "dfe-common.pdb" -}}
{{- $enabled := true -}}
{{- $maxUnavailable := 0 -}}
{{- $minAvailable := 0 -}}
{{- with .Values.pdb -}}
{{- if hasKey . "enabled" }}{{- $enabled = .enabled -}}{{- end -}}
{{- $maxUnavailable = .maxUnavailable | default 0 -}}
{{- $minAvailable = .minAvailable | default 0 -}}
{{- end -}}
{{- $replicas := int (.Values.replicaCount | default 1) -}}
{{- $floor := $replicas -}}
{{- $kedaMin := 0 -}}
{{- if and .Values.keda .Values.keda.enabled -}}
{{- $kedaMin = int (.Values.keda.minReplicaCount | default 1) -}}
{{- $floor = $kedaMin -}}
{{- end -}}
{{- if and $enabled (or (ge $replicas 2) (ge $kedaMin 2)) }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "dfe-common.fullname" . }}
  labels:
    {{- include "dfe-common.labels" . | nindent 4 }}
spec:
  {{- if $maxUnavailable }}
  maxUnavailable: {{ $maxUnavailable }}
  {{- else if $minAvailable }}
  minAvailable: {{ $minAvailable }}
  {{- else if ge $floor 2 }}
  minAvailable: 1
  {{- else }}
  maxUnavailable: 1
  {{- end }}
  selector:
    matchLabels:
      {{- include "dfe-common.selectorLabels" . | nindent 6 }}
{{- end }}
{{- end }}
