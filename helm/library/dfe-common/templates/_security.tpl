{{/*
dfe-common.podSecurityContext — pod-level securityContext per DFE container +
k8s standards (containers.md:618-641, k8s.md:531-534).

Defaults: runAsNonRoot, runAsUser/runAsGroup/fsGroup 1000, seccomp RuntimeDefault.
Override per chart via .Values.podSecurityContext (e.g. otel-collector runs 10001,
kafbat runs 100). IMPORTANT: an image whose USER is a NAME (not a numeric uid)
is rejected by the kubelet under runAsNonRoot unless runAsUser is set numerically
here — so ferretdb (uid 1000) and kafbat (uid 100) MUST carry an explicit runAsUser.

Set .Values.podSecurityContext.enabled=false for a privileged workload that
manages its own securityContext (dfe-vpn).

Usage:
  spec:
    {{- include "dfe-common.podSecurityContext" . | nindent 6 }}
*/}}
{{- define "dfe-common.podSecurityContext" -}}
{{- $sc := .Values.podSecurityContext | default dict -}}
{{- $enabled := true -}}
{{- if hasKey $sc "enabled" -}}{{- $enabled = $sc.enabled -}}{{- end -}}
{{- if $enabled }}
securityContext:
  runAsNonRoot: {{ if hasKey $sc "runAsNonRoot" }}{{ $sc.runAsNonRoot }}{{ else }}true{{ end }}
  runAsUser: {{ $sc.runAsUser | default 1000 }}
  runAsGroup: {{ $sc.runAsGroup | default 1000 }}
  fsGroup: {{ $sc.fsGroup | default 1000 }}
  seccompProfile:
    type: {{ $sc.seccompProfileType | default "RuntimeDefault" }}
{{- end }}
{{- end }}

{{/*
dfe-common.containerSecurityContext — container-level hardening per
containers.md and k8s.md. Drops ALL caps, blocks privilege escalation, makes
the root filesystem read-only by default.

Override via .Values.containerSecurityContext:
  - readOnlyRootFilesystem: false — for a workload that writes its rootfs (add an
    emptyDir for scratch instead where possible).
  - capabilities.add: [NET_ADMIN] — for a workload that legitimately needs a
    capability (dfe-vpn). ALL is still dropped first, so only the listed caps
    remain; this is how SYS_MODULE gets removed — by not listing it.

Usage:
  containers:
    - name: app
      {{- include "dfe-common.containerSecurityContext" . | nindent 6 }}
*/}}
{{- define "dfe-common.containerSecurityContext" -}}
{{- $sc := .Values.containerSecurityContext | default dict -}}
securityContext:
  allowPrivilegeEscalation: {{ if hasKey $sc "allowPrivilegeEscalation" }}{{ $sc.allowPrivilegeEscalation }}{{ else }}false{{ end }}
  readOnlyRootFilesystem: {{ if hasKey $sc "readOnlyRootFilesystem" }}{{ $sc.readOnlyRootFilesystem }}{{ else }}true{{ end }}
  capabilities:
    drop:
      - ALL
{{- with $sc.capabilities }}
{{- with .add }}
    add:
      {{- toYaml . | nindent 6 }}
{{- end }}
{{- end }}
{{- end }}
