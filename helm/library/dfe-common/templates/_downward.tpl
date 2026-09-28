{{/*
dfe-common.podNamespaceEnv -- the pod's namespace as POD_NAMESPACE, from the
downward API.

scalo reads the namespace from POD_NAMESPACE first and falls back to the
service-account token mount, so a pod that mounts no token needs this entry or
its logs carry no namespace.

Usage (container env block, after dfe-common.extraEnv):
  {{- include "dfe-common.podNamespaceEnv" . | nindent 12 }}
*/}}
{{- define "dfe-common.podNamespaceEnv" -}}
- name: POD_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
{{- end -}}

{{/*
dfe-common.serviceAccountTokenMounted -- non-empty when this app mounts its
service-account token, so it reads as a boolean in an `if`.

serviceAccount.mountToken, off by default: no data-plane app calls the
Kubernetes API. The one reason to turn it on is Vault's Kubernetes auth method,
which logs in with that token.
*/}}
{{- define "dfe-common.serviceAccountTokenMounted" -}}
{{- $sa := .Values.serviceAccount | default dict -}}
{{- if $sa.mountToken -}}
true
{{- end -}}
{{- end -}}

{{/*
dfe-common.automountServiceAccountToken -- the field, from the dial above, for
both the ServiceAccount and the pod spec, so an account the operator supplies
follows the same setting.

Usage:
  {{- include "dfe-common.automountServiceAccountToken" . | nindent 6 }}
*/}}
{{- define "dfe-common.automountServiceAccountToken" -}}
automountServiceAccountToken: {{ if (include "dfe-common.serviceAccountTokenMounted" .) }}true{{ else }}false{{ end }}
{{- end -}}

{{/*
dfe-common.serviceAccountFilesVolume / dfe-common.serviceAccountFilesMount --
the service-account directory with ca.crt and namespace in it and no token, for
a pod with automountServiceAccountToken false.

scalo's version check derives its instance id from those two files, so without
them every pod start reports as a new install. ca.crt comes from the
kube-root-ca.crt ConfigMap Kubernetes publishes into every namespace, the same
source the token automount itself projects it from.

Both render nothing when the token is mounted: the automount already puts
ca.crt and namespace at that path, and a second mount there would collide.

Usage (pod spec volumes, and the app container's volumeMounts):
  {{- include "dfe-common.serviceAccountFilesVolume" . | nindent 8 }}
  {{- include "dfe-common.serviceAccountFilesMount" . | nindent 12 }}
*/}}
{{- define "dfe-common.serviceAccountFilesVolume" -}}
{{- if not (include "dfe-common.serviceAccountTokenMounted" .) -}}
- name: serviceaccount-files
  projected:
    sources:
      - configMap:
          name: kube-root-ca.crt
          items:
            - key: ca.crt
              path: ca.crt
      - downwardAPI:
          items:
            - path: namespace
              fieldRef:
                fieldPath: metadata.namespace
{{- end -}}
{{- end -}}

{{- define "dfe-common.serviceAccountFilesMount" -}}
{{- if not (include "dfe-common.serviceAccountTokenMounted" .) -}}
- name: serviceaccount-files
  mountPath: /var/run/secrets/kubernetes.io/serviceaccount
  readOnly: true
{{- end -}}
{{- end -}}
