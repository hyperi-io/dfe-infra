{{/*
dfe-clickhouse.storageModel -- the storage model in force, and the ONLY reading
of it any template takes. Never read .Values.clickhouse.storageModel directly.

An explicit clickhouse.storageModel wins. Left empty the model is derived from
whether the deployment supplies an object store: an endpoint means the preferred
`cached-object`, nothing means `local`. Derivation is skipped under
mode=external, where the supplied ClickHouse owns its storage and a derived
non-local model would fail the render for a value nobody set; an explicit one
still fails there, which is the point of the guard below.

The model is locked at first deploy -- the operator takes no new disk on an
existing ClickHouseCluster, and the deployment repo's
governance/policies/storage-layout.yaml refuses the change afterwards -- so this
derivation decides once.
*/}}
{{- define "dfe-clickhouse.storageModel" -}}
{{- if .Values.clickhouse.storageModel -}}
{{- .Values.clickhouse.storageModel -}}
{{- else if and .Values.clickhouse.objectStore.endpoint (ne .Values.clickhouse.mode "external") -}}
cached-object
{{- else -}}
local
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.storageFamily -- the half of storageModel that says what happens
to the data. `tiered` MOVES parts to the bulk store, so the bulk copy is the only
one and both stores must be durable; `cached` COPIES them, so the local copy is
disposable. Empty for `local`.

The vocabulary is `<family>-<bulk>` by construction, so a new model claims a cell
without editing this; validateStorageModel rejects anything off the list first.
*/}}
{{- define "dfe-clickhouse.storageFamily" -}}
{{- $model := include "dfe-clickhouse.storageModel" . -}}
{{- if ne $model "local" -}}
{{- first (splitList "-" $model) -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.storageBulk -- the half of storageModel that says where the bulk
copy lives: `block` for a second volume, `object` for an object store. Empty for
`local`.
*/}}
{{- define "dfe-clickhouse.storageBulk" -}}
{{- $model := include "dfe-clickhouse.storageModel" . -}}
{{- if ne $model "local" -}}
{{- last (splitList "-" $model) -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.validateStorageModel -- reject an unusable storage model at render
time rather than emitting server config that silently does nothing.

Called from validate.yaml so it fires in every mode, including the ones that
render no ClickHouse object at all.
*/}}
{{- define "dfe-clickhouse.validateStorageModel" -}}
{{- $model := include "dfe-clickhouse.storageModel" . -}}
{{- /* tiered-object and cached-block are named cells in the vocabulary that this
chart does not render yet -- see docs/deployment/storage.md. */ -}}
{{- if not (has $model (list "local" "cached-object" "tiered-block")) -}}
{{- fail (printf "clickhouse.storageModel must be local, cached-object or tiered-block, got %q" $model) -}}
{{- end -}}
{{- if and (ne $model "local") (eq .Values.clickhouse.mode "external") -}}
{{- fail (printf "clickhouse.storageModel=%s is meaningless with mode=external -- the supplied ClickHouse owns its own storage" $model) -}}
{{- end -}}
{{- if eq (include "dfe-clickhouse.storageBulk" .) "object" -}}
{{- if not .Values.clickhouse.objectStore.endpoint -}}
{{- fail (printf "clickhouse.storageModel=%s needs clickhouse.objectStore.endpoint (bucket URL with a trailing slash)" $model) -}}
{{- end -}}
{{- end -}}
{{- if eq (include "dfe-clickhouse.storageBulk" .) "block" -}}
{{- $cold := .Values.clickhouse.tieredBlock.coldName -}}
{{- if not $cold -}}
{{- fail (printf "clickhouse.storageModel=%s needs clickhouse.tieredBlock.coldName -- it names the PVC, the mount, the disk and the cold volume" $model) -}}
{{- end -}}
{{- if eq $cold "default" -}}
{{- fail "clickhouse.tieredBlock.coldName must not be \"default\" -- that is the hot disk, provisioned from clickhouse.storage" -}}
{{- end -}}
{{- /* Volume order is tier priority and the serialised order is alphabetical, so
a cold name sorting first makes the bulk volume the hot tier with no error. */ -}}
{{- if ne (index (sortAlpha (list "default" $cold)) 0) "default" -}}
{{- fail (printf "clickhouse.tieredBlock.coldName %q sorts before \"default\", which silently inverts the tiers -- the bulk volume would become the hot one. Pick a name sorting after \"default\" (e.g. slow, tier2, warm)" $cold) -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.cacheVolume -- where the cached-object cache disk's bytes live:
`pvc` (the current behaviour, and the only one clickhouse-single.yaml renders)
or `instance-store` (cluster mode only -- see templates/clickhouse.yaml).
*/}}
{{- define "dfe-clickhouse.cacheVolume" -}}
{{- .Values.clickhouse.objectStore.cache.volume | default "pvc" -}}
{{- end }}

{{/*
dfe-clickhouse.validateCacheVolume -- reject a cache-volume placement that
cannot be honoured, alongside validateStorageModel above.

instance-store needs an object-store cache to exist at all (storageBulk
"object"), an explicit cacheSize (an emptyDir carries no PVC to size a ratio
against, unlike the pvc volume), and cluster mode -- clickhouse-single.yaml
renders no such volume yet, so requesting it there would silently leave the
cache on the data PVC despite the value claiming otherwise.
*/}}
{{- define "dfe-clickhouse.validateCacheVolume" -}}
{{- $volume := include "dfe-clickhouse.cacheVolume" . -}}
{{- if not (has $volume (list "pvc" "instance-store")) -}}
{{- fail (printf "clickhouse.objectStore.cache.volume must be pvc or instance-store, got %q" $volume) -}}
{{- end -}}
{{- if eq $volume "instance-store" -}}
{{- if ne (include "dfe-clickhouse.storageBulk" .) "object" -}}
{{- fail (printf "clickhouse.objectStore.cache.volume=instance-store needs an object-store cache to place -- clickhouse.storageModel=%s has none" (include "dfe-clickhouse.storageModel" .)) -}}
{{- end -}}
{{- if not .Values.clickhouse.objectStore.cacheSize -}}
{{- fail "clickhouse.objectStore.cache.volume=instance-store needs clickhouse.objectStore.cacheSize set explicitly -- an emptyDir has no PVC to size a ratio against" -}}
{{- end -}}
{{- if ne .Values.clickhouse.mode "cluster" -}}
{{- fail (printf "clickhouse.objectStore.cache.volume=instance-store needs clickhouse.mode=cluster -- clickhouse-single.yaml mounts no such volume for mode=%s" .Values.clickhouse.mode) -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.storageConfiguration -- the server-config fragment that places
MergeTree parts for the non-local storage models.

ONE definition because two paths consume it: the operator CR's
settings.extraConfig (cluster mode) and the config.d ConfigMap (single mode). A
second spelling would give the two modes different on-disk layouts.

An object bulk store carries no credentials here: use_environment_credentials
makes the server read AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from its
environment, which both paths wire from the ESO-materialised Secret.

DISK AND VOLUME NAMES ARE ORDER-BEARING IN BOTH FAMILIES, and they are runtime
identity -- renaming one on a live deployment is a rebuild, not a values edit.
The operator serialises extraConfig as JSON with SORTED KEYS, so the order
written here is discarded. ClickHouse builds a cache disk only after the disk it
wraps, so the cache must sort AFTER its backing disk: `s3_object` then
`s3_object_cache`. A cache sorting earlier fails the server at startup with
BAD_ARGUMENTS "there is no such disk (it should be initialized before cache
disk)" -- proven live 2026-08-30. Under a block bulk store, volume order IS tier
priority, so the cold volume must sort after `default`; validateStorageModel
above fails the render otherwise.

Emits nothing for storageModel: local.
*/}}
{{- define "dfe-clickhouse.storageConfiguration" -}}
{{- $bulk := include "dfe-clickhouse.storageBulk" . -}}
{{- if eq $bulk "object" -}}
storage_configuration:
  disks:
    s3_object:
      type: s3
      endpoint: {{ .Values.clickhouse.objectStore.endpoint | quote }}
      use_environment_credentials: true
      metadata_path: /var/lib/clickhouse/disks/s3_object/
      {{- with .Values.clickhouse.objectStore.region }}
      region: {{ . | quote }}
      {{- end }}
      {{- /* Empty is tri-state, not false: GCS rejects batch delete, so a
      deployment against it must be able to render an explicit false. */ -}}
      {{- if ne (toString .Values.clickhouse.objectStore.supportBatchDelete) "" }}
      support_batch_delete: {{ .Values.clickhouse.objectStore.supportBatchDelete }}
      {{- end }}
      {{- /* Unset leaves the server defaults, which retry for minutes against an
      unreachable store. See clickhouse.objectStore in values.yaml. */ -}}
      {{- with .Values.clickhouse.objectStore.retryAttempts }}
      s3_retry_attempts: {{ . }}
      {{- end }}
      {{- with .Values.clickhouse.objectStore.connectTimeoutMs }}
      s3_connect_timeout_ms: {{ . }}
      {{- end }}
      {{- with .Values.clickhouse.objectStore.requestTimeoutMs }}
      s3_request_timeout_ms: {{ . }}
      {{- end }}
    s3_object_cache:
      type: cache
      disk: s3_object
      path: /var/lib/clickhouse/disks/s3_object_cache/
      {{- /* The server refuses max_size and max_size_ratio_to_total_space
      together with BAD_ARGUMENTS at startup, so this is either/or: a fixed
      cacheSize when the deployment names one, the ratio otherwise. */ -}}
      {{- if .Values.clickhouse.objectStore.cacheSize }}
      max_size: {{ .Values.clickhouse.objectStore.cacheSize | quote }}
      {{- else }}
      max_size_ratio_to_total_space: {{ .Values.clickhouse.objectStore.cache.maxSizeRatioToTotalSpace }}
      {{- end }}
      cache_on_write_operations: {{ .Values.clickhouse.objectStore.cache.onWriteOperations }}
      keep_free_space_size_ratio: {{ .Values.clickhouse.objectStore.cache.keepFreeSpaceSizeRatio }}
      load_metadata_asynchronously: {{ .Values.clickhouse.objectStore.cache.loadMetadataAsynchronously }}
  policies:
    s3_cached:
      volumes:
        main:
          disk: s3_object_cache
# Server-wide default for MergeTree tables, so the engine's DDL needs no
# per-table storage_policy and the model stays a deploy-time decision.
merge_tree:
  storage_policy: s3_cached
{{- else if eq $bulk "block" -}}
{{- $cold := .Values.clickhouse.tieredBlock.coldName -}}
storage_configuration:
  {{- if eq .Values.clickhouse.mode "single" }}
  # Cluster mode gets this from the operator, which registers a disk for every
  # additional volume claim at this same path. Single mode runs no operator, so
  # the cold disk is declared here on the mount the StatefulSet gives it.
  disks:
    {{ $cold }}:
      path: /var/lib/clickhouse/disks/{{ $cold }}/
  {{- end }}
  policies:
    # The hot volume and disk keep the name "default": the server refuses a
    # policy change that drops a volume name it already loaded. The replace
    # attribute is load-bearing in cluster mode -- extraConfig lands in
    # 99-extra-config.yaml, which merges after the operator's
    # 10-storage-jbod.yaml, whose generated policy STRIPES the cold disk
    # alongside the hot one instead of ranking them.
    default:
      "@replace": "1"
      move_factor: {{ .Values.clickhouse.tieredBlock.moveFactor }}
      volumes:
        default:
          disk: default
        {{ $cold }}:
          disk: {{ $cold }}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.objectStoreEnv -- the object-store credential, read from the
environment by both the operator CR and the single-mode StatefulSet.
*/}}
{{- define "dfe-clickhouse.objectStoreEnv" -}}
- name: AWS_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ .Values.clickhouse.objectStore.secretName }}
      key: access_key_id
- name: AWS_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.clickhouse.objectStore.secretName }}
      key: secret_access_key
{{- end }}
