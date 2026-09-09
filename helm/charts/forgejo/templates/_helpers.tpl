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
{{- if .Values.webhook.enabled }}
# Webhook delivery is blocked by default to anything that is not a public
# unicast address (webhook.ALLOWED_HOST_LIST defaults to `external`), and Argo
# CD's Service address is a cluster-private one -- so without this the hook is
# created, looks configured, and every delivery is refused before it leaves the
# process. Scoped to the one host it must reach, not opened to `private`.
- name: GITEA__webhook__ALLOWED_HOST_LIST
  value: {{ .Values.webhook.argocdHost | quote }}
# argocd-server terminates TLS with its own self-signed certificate, which no
# trust store in this cluster carries. The hop is in-cluster to a named Service.
- name: GITEA__webhook__SKIP_TLS_VERIFY
  value: "true"
{{- end }}
{{- end -}}
