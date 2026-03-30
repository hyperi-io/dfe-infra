#!/usr/bin/env bash
# generate-transforms.sh — create all 5 dfe-transform-* Helm charts
# Run once from helm/charts/; idempotent (overwrites if re-run).
set -euo pipefail

CHART_DIR="$(cd "$(dirname "$0")" && pwd)"

TRANSFORMS=(wasm vrl vector elastic splack)

DESCRIPTIONS=(
  "WASM plugin execution transform — Kafka consumer → WASM → Kafka producer"
  "Vector Remap Language transform — Kafka consumer → VRL → Kafka producer"
  "Vector sidecar management transform — Kafka consumer → Vector → Kafka producer"
  "Elasticsearch format conversion transform — Kafka consumer → ES format → Kafka producer"
  "Splunk format conversion transform — Kafka consumer → Splunk format → Kafka producer"
)

for i in "${!TRANSFORMS[@]}"; do
  NAME="${TRANSFORMS[$i]}"
  DESC="${DESCRIPTIONS[$i]}"
  CHART_NAME="dfe-transform-${NAME}"
  DIR="${CHART_DIR}/${CHART_NAME}"

  echo "==> Generating ${CHART_NAME} ..."
  mkdir -p "${DIR}/templates"
  mkdir -p "${DIR}/charts"

  # ── Chart.yaml ────────────────────────────────────────────────────────────
  cat > "${DIR}/Chart.yaml" <<YAML
apiVersion: v2
name: ${CHART_NAME}
description: "${DESC}"
type: application
version: 0.1.0
appVersion: "2.2.0"
dependencies:
  - name: dfe-common
    version: "0.1.0"
    repository: "file://../../library/dfe-common"
YAML

  # ── values.yaml ───────────────────────────────────────────────────────────
  cat > "${DIR}/values.yaml" <<YAML
project: dfe
component: transform-${NAME}
env: local
cloud: local

image:
  repository: ""
  tag: ""
  pullPolicy: IfNotPresent

replicaCount: 1

kafka:
  bootstrapServers: ""
  saslSecretName: dfe-kafka-user
  sourceTopic: ""        # set per deployment via ArgoCD values
  destTopic: ""          # set per deployment
  consumerGroup: ""      # defaults to dfe-transform-${NAME}

otel:
  endpoint: ""

config:
  mountPath: /config
  nfs:
    server: ""
    path: ""

resources:
  requests:
    cpu: 100m
    memory: 128Mi
  limits:
    cpu: 500m
    memory: 512Mi

serviceAccount:
  create: true
  annotations: {}
YAML

  # ── templates/deployment.yaml ─────────────────────────────────────────────
  cat > "${DIR}/templates/deployment.yaml" <<'TMPL'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "dfe-common.fullname" . }}
  labels:
    {{- include "dfe-common.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.replicaCount }}
  selector:
    matchLabels:
      {{- include "dfe-common.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "dfe-common.labels" . | nindent 8 }}
    spec:
      serviceAccountName: {{ include "dfe-common.serviceAccountName" . }}
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        fsGroup: 1000
      containers:
        - name: transform
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          env:
            - name: KAFKA_BOOTSTRAP_SERVERS
              value: {{ .Values.kafka.bootstrapServers | quote }}
            - name: KAFKA_SOURCE_TOPIC
              value: {{ .Values.kafka.sourceTopic | quote }}
            - name: KAFKA_DEST_TOPIC
              value: {{ .Values.kafka.destTopic | quote }}
            - name: KAFKA_CONSUMER_GROUP
              value: {{ .Values.kafka.consumerGroup | default (printf "dfe-%s" .Values.component) | quote }}
            - name: KAFKA_SASL_USERNAME
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.kafka.saslSecretName }}
                  key: username
            - name: KAFKA_SASL_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.kafka.saslSecretName }}
                  key: password
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: {{ .Values.otel.endpoint | quote }}
            - name: OTEL_SERVICE_NAME
              value: {{ printf "dfe-%s" .Values.component | quote }}
          volumeMounts:
            - name: config
              mountPath: {{ .Values.config.mountPath }}
              readOnly: true
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          livenessProbe:
            exec:
              command:
                - pgrep
                - -x
                - transform
            initialDelaySeconds: 10
            periodSeconds: 15
            failureThreshold: 3
      volumes:
        - name: config
          {{- if .Values.config.nfs.server }}
          nfs:
            server: {{ .Values.config.nfs.server }}
            path: {{ .Values.config.nfs.path }}
            readOnly: true
          {{- else }}
          emptyDir: {}
          {{- end }}
TMPL

  # ── templates/serviceaccount.yaml ─────────────────────────────────────────
  cat > "${DIR}/templates/serviceaccount.yaml" <<'TMPL'
{{- if .Values.serviceAccount.create }}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "dfe-common.serviceAccountName" . }}
  labels:
    {{- include "dfe-common.labels" . | nindent 4 }}
  {{- with .Values.serviceAccount.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end }}
TMPL

  echo "    Chart files written."
done

echo ""
echo "All 5 transform charts generated."
