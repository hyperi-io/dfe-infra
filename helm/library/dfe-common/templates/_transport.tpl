{{/*
dfe-common.transport -- which transport carries records between stages here.

`kafka.mode` in the profile values is the deployment's one transport fact:
`disabled` (slim, scale-mesh) means direct point-to-point gRPC, any other mode
means a broker holds records between stages. A chart that compares that string
itself is a second copy of the derivation.

The vocabulary is bus/direct, never a product name -- the same two words a
source's `transport` and the engine's `transport.default` carry -- so a second
bus provider joins without a template change.

`kafka` is absent from some charts' values (dfe-engine has no broker settings of
its own), so the map is guarded before the key is read: a missing map is the
bus, which is the product default.

Usage:
  {{- if eq (include "dfe-common.transport" .) "direct" }}
  {{- if (include "dfe-common.transportIsBus" .) }}
*/}}
{{- define "dfe-common.transport" -}}
{{- $mode := "" -}}
{{- if .Values.kafka -}}
{{- $mode = default "" .Values.kafka.mode -}}
{{- end -}}
{{- if eq $mode "disabled" -}}
direct
{{- else -}}
bus
{{- end -}}
{{- end -}}

{{/*
dfe-common.transportIsBus -- non-empty when a bus carries this deployment, so it
reads as a boolean in an `if`. An empty string is the only falsey value a
template include can return.
*/}}
{{- define "dfe-common.transportIsBus" -}}
{{- if eq (include "dfe-common.transport" .) "bus" -}}
true
{{- end -}}
{{- end -}}
