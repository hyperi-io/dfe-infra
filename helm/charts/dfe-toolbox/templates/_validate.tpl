{{/*
dfe-toolbox.validate -- the render guards; templates/validate.yaml runs them.

Both toolbox.pod.enabled and toolbox.pod.kubeApiAccess gate real behaviour
(replica count, a ServiceAccount with API read access), so a value that is
not a real YAML boolean -- a quoted "false" string is truthy in a Go template
-- would silently do the opposite of what it looks like it says.
*/}}
{{- define "dfe-toolbox.validate" -}}
{{- $pod := .Values.toolbox.pod -}}
{{- range $field := list "enabled" "kubeApiAccess" -}}
{{- $val := index $pod $field -}}
{{- if not (kindIs "bool" $val) -}}
{{- fail (printf "toolbox.pod.%s is %v (a %s) -- it must be a real YAML boolean, true or false, not a quoted string or any other type" $field $val (kindOf $val)) -}}
{{- end -}}
{{- end -}}
{{- if and $pod.kubeApiAccess (not $pod.enabled) -}}
{{- fail "toolbox.pod.kubeApiAccess is true but toolbox.pod.enabled is false -- that would grant a ServiceAccount and read-only API access to a pod that never runs. Set toolbox.pod.enabled: true as well, or turn kubeApiAccess back off." -}}
{{- end -}}
{{- end -}}
