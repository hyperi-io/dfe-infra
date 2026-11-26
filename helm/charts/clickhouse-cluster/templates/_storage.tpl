{{/*
dfe-clickhouse.storageModel -- the storage model in force, and the only reading
of it any template takes. Never read .Values.clickhouse.storageModel directly.

auto, or an empty value, derives it: an objectStore endpoint gives cached-object,
none gives local. An explicit value wins. Derivation is skipped under
mode=external, where the supplied server owns its storage. docs/deployment/storage.md
*/}}
{{- define "dfe-clickhouse.storageModel" -}}
{{- $override := .Values.clickhouse.storageModel -}}
{{- if and $override (ne $override "auto") -}}
{{- $override -}}
{{- else if and .Values.clickhouse.objectStore.endpoint (ne .Values.clickhouse.mode "external") -}}
cached-object
{{- else -}}
local
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.storageFamily -- the first half of storageModel: tiered MOVES parts to the bulk store, cached COPIES them. Empty for local.
*/}}
{{- define "dfe-clickhouse.storageFamily" -}}
{{- $model := include "dfe-clickhouse.storageModel" . -}}
{{- if ne $model "local" -}}
{{- first (splitList "-" $model) -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.storageBulk -- the second half of storageModel: block for a second volume, object for an object store. Empty for local.
*/}}
{{- define "dfe-clickhouse.storageBulk" -}}
{{- $model := include "dfe-clickhouse.storageModel" . -}}
{{- if ne $model "local" -}}
{{- last (splitList "-" $model) -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.validateStorageModel -- reject an unusable storage model at render time. Called from validate.yaml so it fires in every mode.
*/}}
{{- define "dfe-clickhouse.validateStorageModel" -}}
{{- $model := include "dfe-clickhouse.storageModel" . -}}
{{- /* tiered-object and cached-block are not rendered. docs/deployment/storage.md */ -}}
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
{{- /* Volume order is tier priority and the serialised order is alphabetical. */ -}}
{{- if ne (index (sortAlpha (list "default" $cold)) 0) "default" -}}
{{- fail (printf "clickhouse.tieredBlock.coldName %q sorts before \"default\", which silently inverts the tiers -- the bulk volume would become the hot one. Pick a name sorting after \"default\" (e.g. slow, tier2, warm)" $cold) -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.cacheVolume -- pvc, or instance-store (cluster mode only). Single mode renders pvc alone.
*/}}
{{- define "dfe-clickhouse.cacheVolume" -}}
{{- .Values.clickhouse.objectStore.cache.volume | default "pvc" -}}
{{- end }}

{{/*
dfe-clickhouse.instanceStoreCacheMount -- the emptyDir's mount path, outside the data PVC because a nested mount leaves root-owned parents (docs/deployment/storage.md, Extra mounts stay outside the data volume).
*/}}
{{- define "dfe-clickhouse.instanceStoreCacheMount" -}}
/var/lib/clickhouse-cache
{{- end }}

{{/*
dfe-clickhouse.quantityBytes -- a binary storage quantity (87Gi, 500Mi, 1024) as bytes. Anything else fails the render.
*/}}
{{- define "dfe-clickhouse.quantityBytes" -}}
{{- $q := . | toString -}}
{{- if regexMatch "^[0-9]+Ti$" $q -}}{{ mul (int64 (trimSuffix "Ti" $q)) 1099511627776 }}
{{- else if regexMatch "^[0-9]+Gi$" $q -}}{{ mul (int64 (trimSuffix "Gi" $q)) 1073741824 }}
{{- else if regexMatch "^[0-9]+Mi$" $q -}}{{ mul (int64 (trimSuffix "Mi" $q)) 1048576 }}
{{- else if regexMatch "^[0-9]+Ki$" $q -}}{{ mul (int64 (trimSuffix "Ki" $q)) 1024 }}
{{- else if regexMatch "^[0-9]+$" $q -}}{{ int64 $q }}
{{- else -}}
{{- fail (printf "clickhouse.objectStore.cacheSize must be a whole number of bytes, Ki, Mi, Gi or Ti under cache.volume=instance-store, got %q" $q) -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.wholeNumber -- (list name value) as a plain non-negative integer, or a failed render naming the key. A values-file number arrives as a float, which prints in exponent form past a million.
*/}}
{{- define "dfe-clickhouse.wholeNumber" -}}
{{- $name := index . 0 -}}
{{- $v := index . 1 -}}
{{- if and (kindIs "float64" $v) (ge $v 0.0) (eq $v (floor $v)) -}}
{{- int64 $v -}}
{{- else if regexMatch "^[0-9]+$" (toString $v) -}}
{{- toString $v -}}
{{- else -}}
{{- fail (printf "%s must be a whole number, got %v" $name $v) -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.instanceStoreCacheBytes -- the cache's max_size: cache.maxSizeRatioToSizeLimit of cacheSize.
*/}}
{{- define "dfe-clickhouse.instanceStoreCacheBytes" -}}
{{- $limit := include "dfe-clickhouse.quantityBytes" .Values.clickhouse.objectStore.cacheSize | int64 -}}
{{- int64 (floor (mulf (float64 $limit) (float64 .Values.clickhouse.objectStore.cache.maxSizeRatioToSizeLimit))) -}}
{{- end }}

{{/*
dfe-clickhouse.validateCacheVolume -- reject a placement that cannot be honoured. instance-store needs an object bulk store, an explicit cacheSize and cluster mode.
*/}}
{{- define "dfe-clickhouse.validateCacheVolume" -}}
{{- $volume := include "dfe-clickhouse.cacheVolume" . -}}
{{- if not (has $volume (list "pvc" "instance-store")) -}}
{{- fail (printf "clickhouse.objectStore.cache.volume must be pvc or instance-store, got %q" $volume) -}}
{{- end -}}
{{- if eq $volume "instance-store" -}}
{{- if ne (include "dfe-clickhouse.storageBulk" .) "object" -}}
{{- /* Under auto the model is local only because no endpoint arrived, so name the endpoint. */ -}}
{{- if and (has .Values.clickhouse.storageModel (list "auto" "")) (not .Values.clickhouse.objectStore.endpoint) -}}
{{- fail "clickhouse.objectStore.cache.volume=instance-store needs an object-store cache to place, and no clickhouse.objectStore.endpoint is set -- so clickhouse.storageModel derived to local. The deployment's object-store bucket never reached the chart." -}}
{{- end -}}
{{- fail (printf "clickhouse.objectStore.cache.volume=instance-store needs an object-store cache to place -- clickhouse.storageModel=%s has none" (include "dfe-clickhouse.storageModel" .)) -}}
{{- end -}}
{{- if not .Values.clickhouse.objectStore.cacheSize -}}
{{- fail "clickhouse.objectStore.cache.volume=instance-store needs clickhouse.objectStore.cacheSize set explicitly -- an emptyDir has no PVC to size a ratio against" -}}
{{- end -}}
{{- $fill := float64 .Values.clickhouse.objectStore.cache.maxSizeRatioToSizeLimit -}}
{{- if or (le $fill 0.0) (ge $fill 1.0) -}}
{{- fail (printf "clickhouse.objectStore.cache.maxSizeRatioToSizeLimit must sit between 0 and 1, got %v -- a cache filling its emptyDir gets the pod evicted" .Values.clickhouse.objectStore.cache.maxSizeRatioToSizeLimit) -}}
{{- end -}}
{{- if ne .Values.clickhouse.mode "cluster" -}}
{{- fail (printf "clickhouse.objectStore.cache.volume=instance-store needs clickhouse.mode=cluster -- clickhouse-single.yaml mounts no such volume for mode=%s" .Values.clickhouse.mode) -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.validateObjectStoreTuning -- reject a burst with no rate beside it: the server reads the burst only when the rate is set.
*/}}
{{- define "dfe-clickhouse.validateObjectStoreTuning" -}}
{{- $tuning := .Values.clickhouse.objectStoreTuning -}}
{{- range $verb := list "Put" "Get" -}}
{{- if and (index $tuning (printf "max%sBurst" $verb)) (not (index $tuning (printf "max%sRps" $verb))) -}}
{{- fail (printf "clickhouse.objectStoreTuning.max%sBurst needs clickhouse.objectStoreTuning.max%sRps -- ClickHouse ignores a burst with no rate" $verb $verb) -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.storageConfiguration -- the server config that places MergeTree parts, for the operator CR's extraConfig (cluster) and the config.d ConfigMap (single). Emits nothing for local.

The object store carries no credentials here: use_environment_credentials reads them from the pod's environment (objectStoreEnv below, or Pod Identity).

Disk and volume names are order-bearing: the operator serialises extraConfig with sorted keys, so a cache disk must sort after the disk it wraps (s3_object, then s3_object_cache) and a cold volume after `default`. Renaming one on a live deployment is a rebuild. docs/deployment/storage.md, Runtime identity.
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
      {{- /* Empty is not false: only an explicit false is rendered. */ -}}
      {{- if ne (toString .Values.clickhouse.objectStore.supportBatchDelete) "" }}
      support_batch_delete: {{ .Values.clickhouse.objectStore.supportBatchDelete }}
      {{- end }}
      {{- with .Values.clickhouse.objectStore.retryAttempts }}
      s3_retry_attempts: {{ . }}
      {{- end }}
      {{- /* The disk reads its HTTP timeouts unprefixed. An s3_ spelling is ignored. */ -}}
      {{- with .Values.clickhouse.objectStore.connectTimeoutMs }}
      connect_timeout_ms: {{ . }}
      {{- end }}
      {{- with .Values.clickhouse.objectStore.requestTimeoutMs }}
      request_timeout_ms: {{ . }}
      {{- end }}
      {{- $tuning := .Values.clickhouse.objectStoreTuning }}
      {{- range $key := list "maxPutRps" "maxPutBurst" "maxGetRps" "maxGetBurst" }}
      {{- with index $tuning $key }}
      s3_{{ snakecase $key }}: {{ include "dfe-clickhouse.wholeNumber" (list (printf "clickhouse.objectStoreTuning.%s" $key) .) }}
      {{- end }}
      {{- end }}
    s3_object_cache:
      type: cache
      disk: s3_object
      {{- if eq (include "dfe-clickhouse.cacheVolume" .) "instance-store" }}
      path: {{ include "dfe-clickhouse.instanceStoreCacheMount" . }}/s3_object_cache/
      {{- else }}
      path: /var/lib/clickhouse/disks/s3_object_cache/
      {{- end }}
      {{- /* The server refuses max_size with max_size_ratio_to_total_space, so it is one or the other. */ -}}
      {{- if eq (include "dfe-clickhouse.cacheVolume" .) "instance-store" }}
      max_size: {{ include "dfe-clickhouse.instanceStoreCacheBytes" . | quote }}
      {{- else if .Values.clickhouse.objectStore.cacheSize }}
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
  {{- /* Each file in a part is an object, so a Wide part costs a request per column stream. */ -}}
  {{- with .Values.clickhouse.objectStoreTuning.minBytesForWidePart }}
  min_bytes_for_wide_part: {{ include "dfe-clickhouse.wholeNumber" (list "clickhouse.objectStoreTuning.minBytesForWidePart" .) }}
  {{- end }}
  {{- with .Values.clickhouse.objectStoreTuning.mergeMaxDynamicSubcolumnsInWidePart }}
  merge_max_dynamic_subcolumns_in_wide_part: {{ include "dfe-clickhouse.wholeNumber" (list "clickhouse.objectStoreTuning.mergeMaxDynamicSubcolumnsInWidePart" .) }}
  {{- end }}
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
dfe-clickhouse.objectStoreEnv -- the object-store credential env vars, for the operator CR and the single-mode StatefulSet.
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
