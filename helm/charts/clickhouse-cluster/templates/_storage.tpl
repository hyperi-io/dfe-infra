{{/*
dfe-clickhouse.validateStorageModel -- reject an unusable storage model at render
time rather than emitting server config that silently does nothing.

Called from validate.yaml so it fires in every mode, including the ones that
render no ClickHouse object at all.
*/}}
{{- define "dfe-clickhouse.validateStorageModel" -}}
{{- $model := .Values.clickhouse.storageModel -}}
{{- if not (has $model (list "local" "s3backed")) -}}
{{- fail (printf "clickhouse.storageModel must be local or s3backed, got %q" $model) -}}
{{- end -}}
{{- if eq $model "s3backed" -}}
{{- if eq .Values.clickhouse.mode "external" -}}
{{- fail "clickhouse.storageModel=s3backed is meaningless with mode=external -- the supplied ClickHouse owns its own storage" -}}
{{- end -}}
{{- if not .Values.clickhouse.s3.endpoint -}}
{{- fail "clickhouse.storageModel=s3backed needs clickhouse.s3.endpoint (bucket URL with a trailing slash)" -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.storageConfiguration -- the server-config fragment that puts
MergeTree parts on the object store behind a local read-through cache.

ONE definition because two paths consume it: the operator CR's
settings.extraConfig (cluster mode) and the config.d ConfigMap (single mode). A
second spelling would give the two modes different on-disk layouts.

Credentials are deliberately absent: use_environment_credentials makes the
server read AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from its environment,
which both paths wire from the ESO-materialised Secret.

DISK NAMES ARE ORDER-BEARING. ClickHouse builds a cache disk only after the disk
it wraps, and the operator serialises extraConfig as JSON with SORTED KEYS, so
the order written here is discarded. The cache must therefore sort AFTER its
backing disk: `s3_object` then `s3_object_cache`. Naming the cache anything that
sorts earlier fails the server at startup with BAD_ARGUMENTS "there is no such
disk (it should be initialized before cache disk)" -- proven live 2026-08-30.

Emits nothing unless storageModel is s3backed.
*/}}
{{- define "dfe-clickhouse.storageConfiguration" -}}
{{- if eq .Values.clickhouse.storageModel "s3backed" -}}
storage_configuration:
  disks:
    s3_object:
      type: s3
      endpoint: {{ .Values.clickhouse.s3.endpoint | quote }}
      use_environment_credentials: true
      metadata_path: /var/lib/clickhouse/disks/s3_object/
      {{- with .Values.clickhouse.s3.region }}
      region: {{ . | quote }}
      {{- end }}
    s3_object_cache:
      type: cache
      disk: s3_object
      path: /var/lib/clickhouse/disks/s3_object_cache/
      max_size: {{ .Values.clickhouse.s3.cacheSize | quote }}
  policies:
    s3_cached:
      volumes:
        main:
          disk: s3_object_cache
# Server-wide default for MergeTree tables, so the engine's DDL needs no
# per-table storage_policy and the model stays a deploy-time decision.
merge_tree:
  storage_policy: s3_cached
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.s3Env -- the object-store credential, read from the environment
by both the operator CR and the single-mode StatefulSet.
*/}}
{{- define "dfe-clickhouse.s3Env" -}}
- name: AWS_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ .Values.clickhouse.s3.secretName }}
      key: access_key_id
- name: AWS_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.clickhouse.s3.secretName }}
      key: secret_access_key
{{- end }}
