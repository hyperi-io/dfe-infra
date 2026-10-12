{{/*
dfe-common.kedaShimAddress -- the in-cluster address of the dfe-keda-shim Service,
derived from THIS release's namespace. dfe-extras names that Service
`dfe-keda-shim` unprefixed and serves it on 8080 (kedaShim.port), and it is
deployed into the same namespace as the apps that poll it, so a deployment in any
namespace renders an address that resolves without a values override. Callers keep
their own override key and fall back to this:

  {{ .Values.keda.pressure.shimAddress | default (include "dfe-common.kedaShimAddress" .) }}
*/}}
{{- define "dfe-common.kedaShimAddress" -}}
{{- printf "dfe-keda-shim.%s.svc.cluster.local:8080" .Release.Namespace -}}
{{- end -}}
