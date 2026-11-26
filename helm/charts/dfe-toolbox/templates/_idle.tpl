{{/*
dfe-toolbox.idleManaged -- "true" when either limit is set, which is what renders the
scale-down CronJob, the activity hook in the pod and the scripts ConfigMap.
*/}}
{{- define "dfe-toolbox.idleManaged" -}}
{{- if or (toString .Values.toolbox.pod.ttlSeconds) (toString .Values.toolbox.pod.idleSeconds) -}}true{{- end -}}
{{- end -}}

{{/* The pod's activity hook: ENV for an interactive sh, PROMPT_COMMAND for bash. */}}
{{- define "dfe-toolbox.activityEnv" -}}
- name: ENV
  value: /opt/dfe-idle/activity.sh
- name: PROMPT_COMMAND
  value: "printf '%(%s)T\\n' -1 2>/dev/null >/tmp/.dfe-toolbox-activity.$$ && mv -f /tmp/.dfe-toolbox-activity.$$ /tmp/.dfe-toolbox-activity"
{{- end -}}
