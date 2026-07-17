{{/*
dfe-kafka.credentialKey -- where the DFE service user's credential lives in the
secrets store.

ONE definition, because the key is written by the PushSecret and read back by two
different ExternalSecrets; if they ever disagree the credential silently fails to
arrive and the broker just never authenticates.

Shape follows the convention the mode=external secret already uses
(externalsecret.yaml): <project>/<env>/kafka/<provider>, RELATIVE to the store's own
mount. dfe-secret-store pins that mount itself (spec.provider.vault.path=secret), so
the key must NOT repeat it -- ESO resolves this to <mount>/data/<key>. Deliberately
provider-agnostic: the same key shape works when the store swaps to aws/gcp/azure.

Usage:
  remoteKey: {{ include "dfe-kafka.credentialKey" . }}
*/}}
{{- define "dfe-kafka.credentialKey" -}}
{{ .Values.project }}/{{ .Values.env }}/kafka/{{ .Values.kafka.provider }}
{{- end }}
