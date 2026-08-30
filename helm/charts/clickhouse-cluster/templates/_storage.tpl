{{/*
dfe-clickhouse.validateStorageModel -- reject an unusable storage model at render
time rather than emitting server config that silently does nothing.

Called from validate.yaml so it fires in every mode, including the ones that
render no ClickHouse object at all.
*/}}
{{- define "dfe-clickhouse.validateStorageModel" -}}
{{- $model := .Values.clickhouse.storageModel -}}
{{- if not (has $model (list "local" "s3backed" "tiered")) -}}
{{- fail (printf "clickhouse.storageModel must be local, s3backed or tiered, got %q" $model) -}}
{{- end -}}
{{- if and (ne $model "local") (eq .Values.clickhouse.mode "external") -}}
{{- fail (printf "clickhouse.storageModel=%s is meaningless with mode=external -- the supplied ClickHouse owns its own storage" $model) -}}
{{- end -}}
{{- if eq $model "s3backed" -}}
{{- if not .Values.clickhouse.s3.endpoint -}}
{{- fail "clickhouse.storageModel=s3backed needs clickhouse.s3.endpoint (bucket URL with a trailing slash)" -}}
{{- end -}}
{{- end -}}
{{- if eq $model "tiered" -}}
{{- $cold := .Values.clickhouse.tiered.coldName -}}
{{- if not $cold -}}
{{- fail "clickhouse.storageModel=tiered needs clickhouse.tiered.coldName -- it names the PVC, the mount, the disk and the cold volume" -}}
{{- end -}}
{{- if eq $cold "default" -}}
{{- fail "clickhouse.tiered.coldName must not be \"default\" -- that is the hot disk, provisioned from clickhouse.storage" -}}
{{- end -}}
{{- /* Volume order is tier priority and the serialised order is alphabetical, so
a cold name sorting first makes the bulk volume the hot tier with no error. */ -}}
{{- if ne (index (sortAlpha (list "default" $cold)) 0) "default" -}}
{{- fail (printf "clickhouse.tiered.coldName %q sorts before \"default\", which silently inverts the tiers -- the bulk volume would become the hot one. Pick a name sorting after \"default\" (e.g. slow, tier2, warm)" $cold) -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.storageConfiguration -- the server-config fragment that places
MergeTree parts for the non-local storage models.

ONE definition because two paths consume it: the operator CR's
settings.extraConfig (cluster mode) and the config.d ConfigMap (single mode). A
second spelling would give the two modes different on-disk layouts.

s3backed: credentials are deliberately absent -- use_environment_credentials
makes the server read AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from its
environment, which both paths wire from the ESO-materialised Secret.

DISK AND VOLUME NAMES ARE ORDER-BEARING IN BOTH MODELS. The operator serialises
extraConfig as JSON with SORTED KEYS, so the order written here is discarded.
ClickHouse builds a cache disk only after the disk it wraps, so the s3backed
cache must sort AFTER its backing disk: `s3_object` then `s3_object_cache`. A
cache sorting earlier fails the server at startup with BAD_ARGUMENTS "there is
no such disk (it should be initialized before cache disk)" -- proven live
2026-08-30. In tiered, volume order IS tier priority, so the cold volume must
sort after `default`; validateStorageModel above fails the render otherwise.

Emits nothing for storageModel: local.
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
      {{- /* Unset leaves the server defaults, which retry for minutes against an
      unreachable store. See clickhouse.s3 in values.yaml for the measurements. */ -}}
      {{- with .Values.clickhouse.s3.retryAttempts }}
      s3_retry_attempts: {{ . }}
      {{- end }}
      {{- with .Values.clickhouse.s3.connectTimeoutMs }}
      s3_connect_timeout_ms: {{ . }}
      {{- end }}
      {{- with .Values.clickhouse.s3.requestTimeoutMs }}
      s3_request_timeout_ms: {{ . }}
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
{{- else if eq .Values.clickhouse.storageModel "tiered" -}}
{{- $cold := .Values.clickhouse.tiered.coldName -}}
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
      move_factor: {{ .Values.clickhouse.tiered.moveFactor }}
      volumes:
        default:
          disk: default
        {{ $cold }}:
          disk: {{ $cold }}
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
