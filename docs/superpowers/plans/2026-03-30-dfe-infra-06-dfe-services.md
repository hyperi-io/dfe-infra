# DFE Infra 06 — DFE Application Service Charts

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create Helm charts for all DFE application services (dfe-engine, dfe-ui, dfe-receiver, dfe-loader, dfe-archiver, dfe-fetcher) deployed at wave 5 by the layer2-apps ApplicationSet.

**Architecture:** Each DFE service gets a Helm chart under `helm/charts/` following the same pattern: Deployment + Service + ServiceAccount, using `dfe-common` for labels/names. Config mounts from NFS/cloud storage. OTel OTLP endpoint for telemetry. Workload identity annotations from cluster secret. All charts read from `common.yaml` + cloud overrides.

**Tech Stack:** Helm 3, dfe-common library chart, Kubernetes Deployment/Service/ServiceAccount

---

## File Structure

```
helm/charts/
├── dfe-engine/           # Python 3.12 FastAPI control plane
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── deployment.yaml
│       ├── service.yaml
│       └── serviceaccount.yaml
│
├── dfe-ui/               # Next.js UI + HyperDX integration
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── deployment.yaml
│       └── service.yaml
│
├── dfe-receiver/         # Rust ingest service (listens on 100.64.x.x)
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── deployment.yaml
│       ├── service.yaml
│       └── serviceaccount.yaml
│
├── dfe-loader/           # Rust → ClickHouse writer
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── deployment.yaml
│       └── serviceaccount.yaml
│
├── dfe-archiver/         # Rust → S3/MinIO archiver
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── deployment.yaml
│       └── serviceaccount.yaml
│
└── dfe-fetcher/          # Rust API fetcher
    ├── Chart.yaml
    ├── values.yaml
    └── templates/
        ├── deployment.yaml
        └── serviceaccount.yaml
```

---

## Implementation Note

All 6 charts follow the same structural pattern. Key differences per service:

| Service | Ports | Config Mount | Special |
|---------|-------|-------------|---------|
| dfe-engine | 8000 (HTTP) | /config (NFS/cloud) | ClickHouse creds, JWT secret, RBAC |
| dfe-ui | 3000 (HTTP) | none | NEXT_PUBLIC_* env vars, HyperDX URL |
| dfe-receiver | 8080 (HTTP), 8443 (gRPC) | /config | Listens on 100.64.x.x, Kafka producer |
| dfe-loader | none (consumer) | /config | Kafka consumer, ClickHouse writer |
| dfe-archiver | none (consumer) | /config | Kafka consumer, S3/MinIO writer |
| dfe-fetcher | none (scheduled) | /config | API creds via ESO, Kafka producer |

---

## Chunk 1: dfe-engine + dfe-ui + dfe-receiver

### Task 1: dfe-engine Chart

**Files:** Create `helm/charts/dfe-engine/` with Chart.yaml, values.yaml, templates/deployment.yaml, templates/service.yaml, templates/serviceaccount.yaml

- [ ] **Step 1: Create all files**

  **Chart.yaml:**
  ```yaml
  apiVersion: v2
  name: dfe-engine
  description: DFE control plane — Python 3.12 FastAPI, YAML SSoT, HelmValuesCompiler
  type: application
  version: 0.1.0
  appVersion: "2.2.0"
  dependencies:
    - name: dfe-common
      version: "0.1.0"
      repository: "file://../../library/dfe-common"
  ```

  **values.yaml:**
  ```yaml
  project: dfe
  component: engine
  env: local
  cloud: local

  image:
    repository: ""     # set from global.registry
    tag: ""
    pullPolicy: IfNotPresent

  replicaCount: 1

  service:
    type: ClusterIP
    port: 8000

  config:
    mountPath: /config
    # NFS mount for local, cloud storage for others
    nfs:
      server: ""
      path: ""

  clickhouse:
    host: ""
    port: 8123
    database: dfe

  postgresql:
    host: ""
    port: 5432
    database: dfe
    secretName: dfe-pg-app

  otel:
    endpoint: ""

  auth:
    jwtSecretName: dfe-engine-jwt
    oidcEnabled: false

  resources:
    requests:
      cpu: 200m
      memory: 256Mi
    limits:
      cpu: "1"
      memory: 1Gi

  serviceAccount:
    create: true
    annotations: {}   # workload identity annotations injected here
  ```

  **templates/serviceaccount.yaml:**
  ```yaml
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
  ```

  **templates/deployment.yaml:**
  ```yaml
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
        containers:
          - name: engine
            image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
            imagePullPolicy: {{ .Values.image.pullPolicy }}
            ports:
              - name: http
                containerPort: 8000
            env:
              - name: DFE_ENGINE_CLICKHOUSE_HOST
                value: {{ .Values.clickhouse.host }}
              - name: DFE_ENGINE_CLICKHOUSE_PORT
                value: {{ .Values.clickhouse.port | quote }}
              - name: DFE_ENGINE_CLICKHOUSE_DATABASE
                value: {{ .Values.clickhouse.database }}
              - name: DFE_ENGINE_PG_HOST
                value: {{ .Values.postgresql.host }}
              - name: DFE_ENGINE_PG_PORT
                value: {{ .Values.postgresql.port | quote }}
              - name: OTEL_EXPORTER_OTLP_ENDPOINT
                value: {{ .Values.otel.endpoint }}
              - name: OTEL_SERVICE_NAME
                value: dfe-engine
            volumeMounts:
              - name: config
                mountPath: {{ .Values.config.mountPath }}
                readOnly: true
            resources:
              {{- toYaml .Values.resources | nindent 14 }}
            livenessProbe:
              httpGet:
                path: /health/live
                port: http
              initialDelaySeconds: 10
            readinessProbe:
              httpGet:
                path: /health/ready
                port: http
              initialDelaySeconds: 5
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
  ```

  **templates/service.yaml:**
  ```yaml
  apiVersion: v1
  kind: Service
  metadata:
    name: {{ include "dfe-common.fullname" . }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    type: {{ .Values.service.type }}
    ports:
      - port: {{ .Values.service.port }}
        targetPort: http
        name: http
    selector:
      {{- include "dfe-common.selectorLabels" . | nindent 4 }}
  ```

- [ ] **Step 2: Lint**
  ```bash
  cd helm/charts/dfe-engine && helm dependency update && helm lint . && helm template test .
  ```

- [ ] **Step 3: Commit**
  ```bash
  git add helm/charts/dfe-engine/
  git commit -m "feat: add dfe-engine chart (Python FastAPI control plane)"
  ```

---

### Task 2: dfe-ui Chart

Same pattern but simpler — no config mount, no ServiceAccount (no cloud API access). Includes NEXT_PUBLIC_* env vars for HyperDX integration.

- [ ] **Step 1: Create all files**

  **Chart.yaml:** name: dfe-ui, appVersion: "2.2.0", depends on dfe-common

  **values.yaml:**
  ```yaml
  project: dfe
  component: ui
  env: local
  cloud: local
  image:
    repository: ""
    tag: ""
    pullPolicy: IfNotPresent
  replicaCount: 1
  service:
    type: ClusterIP
    port: 3000
  env:
    NEXT_PUBLIC_API_URL: ""        # https://dfe.{domain}/api
    NEXT_PUBLIC_HYPERDX_URL: ""    # https://hyperdx.{domain}
    NEXTAUTH_SECRET_NAME: dfe-ui-nextauth
  otel:
    endpoint: ""
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      cpu: 500m
      memory: 512Mi
  ```

  **templates/deployment.yaml:** Standard Deployment with NEXT_PUBLIC_* env vars from values. Health probes on port 3000.

  **templates/service.yaml:** ClusterIP on port 3000.

- [ ] **Step 2: Lint + commit**
  ```bash
  git add helm/charts/dfe-ui/ && git commit -m "feat: add dfe-ui chart (Next.js UI + HyperDX integration)"
  ```

---

### Task 3: dfe-receiver Chart

Rust ingest service. Listens on 100.64.x.x (CGNAT range). Kafka producer. Config mount.

- [ ] **Step 1: Create all files**

  **values.yaml key differences:**
  ```yaml
  component: receiver
  service:
    type: ClusterIP
    httpPort: 8080
    grpcPort: 8443
  receiver:
    listenAddress: "100.64.0.1"   # CGNAT range per spec Section 2.9
  kafka:
    bootstrapServers: ""
    saslSecretName: dfe-kafka-user
  ```

  **templates/deployment.yaml:** Two container ports (http 8080, grpc 8443). Env vars: KAFKA_BOOTSTRAP_SERVERS, DFE_RECEIVER_LISTEN_ADDRESS, OTEL_EXPORTER_OTLP_ENDPOINT. Config volume mount.

  **templates/service.yaml:** Exposes both ports.

  **templates/serviceaccount.yaml:** Same pattern as dfe-engine.

- [ ] **Step 2: Lint + commit**
  ```bash
  git add helm/charts/dfe-receiver/ && git commit -m "feat: add dfe-receiver chart (Rust ingest, 100.64.x.x, Kafka producer)"
  ```

---

## Chunk 2: dfe-loader + dfe-archiver + dfe-fetcher + Update versions.yaml

### Task 4: dfe-loader Chart

Rust Kafka consumer → ClickHouse async insert. No service (headless consumer).

- [ ] **Step 1: Create files** — Deployment only (no Service). Env: KAFKA_BOOTSTRAP_SERVERS, CLICKHOUSE_HOST, OTEL endpoint. ServiceAccount for workload identity.

- [ ] **Step 2: Lint + commit**
  ```bash
  git add helm/charts/dfe-loader/ && git commit -m "feat: add dfe-loader chart (Rust Kafka→ClickHouse writer)"
  ```

---

### Task 5: dfe-archiver Chart

Rust Kafka consumer → S3/MinIO archiver. Same pattern as loader.

- [ ] **Step 1: Create files** — Deployment only. Env: KAFKA_BOOTSTRAP_SERVERS, S3_ENDPOINT, S3_BUCKET, OTEL endpoint.

- [ ] **Step 2: Lint + commit**
  ```bash
  git add helm/charts/dfe-archiver/ && git commit -m "feat: add dfe-archiver chart (Rust Kafka→S3/MinIO archiver)"
  ```

---

### Task 6: dfe-fetcher Chart

Rust scheduled API fetcher → Kafka producer. API credentials via ESO.

- [ ] **Step 1: Create files** — Deployment only. Env: KAFKA_BOOTSTRAP_SERVERS, API creds from ESO secret, OTEL endpoint.

- [ ] **Step 2: Lint + commit**
  ```bash
  git add helm/charts/dfe-fetcher/ && git commit -m "feat: add dfe-fetcher chart (Rust API fetcher→Kafka producer)"
  ```

---

### Task 7: Update versions.yaml with DFE App Versions

- [ ] **Step 1: Add apps section to versions.yaml**

  Replace `apps: {}` with:
  ```yaml
  apps:
    dfe-engine: "2.2.0"
    dfe-ui: "2.2.0"
    dfe-receiver: "2.2.0"
    dfe-loader: "2.2.0"
    dfe-archiver: "2.2.0"
    dfe-fetcher: "2.2.0"
  ```

- [ ] **Step 2: Commit**
  ```bash
  git add versions.yaml && git commit -m "feat: add DFE app versions to versions.yaml SSOT"
  ```

---

## Completion Criteria

- [ ] `helm lint` passes for all 6 service charts
- [ ] `helm template` renders valid Deployment + Service + ServiceAccount for each
- [ ] `versions.yaml` apps section populated
- [ ] All committed and pushed to main

**Next plan:** 07 (AWS EKS)
