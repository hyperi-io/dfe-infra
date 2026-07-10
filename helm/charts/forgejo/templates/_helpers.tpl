{{/*
Shared environment for the Gitea init + main containers.
GITEA__<section>__<KEY> entries are rendered into app.ini by environment-to-ini.
Admin credentials come from the bootstrap-created secret (admin.secretName).
*/}}
{{- define "gitea.env" -}}
- name: GITEA_WORK_DIR
  value: /data
- name: GITEA_CUSTOM
  value: /data/gitea
- name: GITEA__database__DB_TYPE
  value: sqlite3
- name: GITEA__database__PATH
  value: /data/gitea/gitea.db
- name: GITEA__server__HTTP_PORT
  value: "3000"
- name: GITEA__server__DOMAIN
  value: {{ printf "%s.%s.svc.cluster.local" (include "dfe-common.fullname" .) .Release.Namespace | quote }}
- name: GITEA__server__ROOT_URL
  value: {{ printf "http://%s.%s.svc.cluster.local:3000/" (include "dfe-common.fullname" .) .Release.Namespace | quote }}
- name: GITEA__security__INSTALL_LOCK
  value: "true"
- name: GITEA__service__DISABLE_REGISTRATION
  value: "true"
- name: GITEA__repository__DEFAULT_BRANCH
  value: {{ .Values.deployRepo.defaultBranch | quote }}
- name: GITEA_ADMIN_USER
  valueFrom:
    secretKeyRef:
      name: {{ .Values.admin.secretName }}
      key: username
- name: GITEA_ADMIN_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.admin.secretName }}
      key: password
- name: GITEA_ADMIN_EMAIL
  value: {{ .Values.admin.email | quote }}
{{- end -}}
