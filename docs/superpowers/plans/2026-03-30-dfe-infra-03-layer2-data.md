# DFE Infra 03 — Layer 2 Data Platform

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create Helm charts for all wave 4 data platform components (CNPG PG17, Strimzi Kafka, ClickHouse, FerretDB, OTel Collector, HyperDX) so ArgoCD can deploy them via the existing layer2-data ApplicationSet.

**Architecture:** Each data component gets a Helm chart under `helm/charts/`. CRD-based components (CNPG, Strimzi, ClickHouse) are thin charts that template the operator CRDs with values from `common.yaml` + cloud overrides. Standalone components (FerretDB, OTel Collector, HyperDX) use Deployment/Service templates. All charts depend on `dfe-common` for labels. Versions go in `versions.yaml` under the `data:` section.

**Tech Stack:** Helm 3, CNPG operator CRDs, Strimzi CRDs, Altinity ClickHouse operator CRDs, OpenTelemetry Collector, HyperDX, FerretDB

**Depends on:** Plan 02 (operators deployed at wave 3 — CNPG, Strimzi, ClickHouse operators must be running before wave 4 CRDs are applied)

---

## Prerequisite: What Already Exists

- `argocd/appsets/layer2-data.yaml` — ApplicationSet referencing `helm/charts/{app}` for 6 components
- `argocd/values/common.yaml` — shared values (kafka brokers, clickhouse host, pg host, otel endpoint)
- `argocd/values/local.yaml` — Rancher local overrides
- `helm/library/dfe-common/` — shared label helpers
- `versions.yaml` — SSOT for versions (data section currently empty)
- Operators running at wave 3: CNPG v0.27.1, Strimzi v0.50.1, ClickHouse Operator v0.23.0

---

## File Structure

```
helm/charts/
├── cnpg-cluster/             # CNPG Cluster CRD + ScheduledBackup CRD
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── cluster.yaml      # CNPG Cluster CRD (PG17, 3 instances, WAL archiving)
│       └── scheduled-backup.yaml  # ScheduledBackup CRD (to S3/MinIO)
│
├── strimzi-kafka/            # Strimzi Kafka CRD + KafkaUser CRDs
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── kafka.yaml        # Kafka CRD (KRaft, 3 brokers, SASL/SCRAM, zstd)
│       └── kafka-user.yaml   # KafkaUser for DFE services (SCRAM-SHA-512)
│
├── clickhouse-cluster/       # ClickHouse Installation CRD
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       └── clickhouse.yaml   # ClickHouseInstallation CRD (1-3 replicas, tiered roles)
│
├── ferretdb/                 # FerretDB Deployment + Service
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── deployment.yaml
│       └── service.yaml
│
├── otel-collector/           # OTel Collector (DaemonSet + Gateway)
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── daemonset.yaml    # Node-level collector (logs, metrics)
│       ├── gateway-deployment.yaml  # Central gateway (OTLP → ClickHouse/HyperDX)
│       ├── gateway-service.yaml     # ClusterIP service for gateway
│       └── configmap.yaml    # OTel Collector config (pipelines, exporters)
│
└── hyperdx/                  # HyperDX Deployment + Service
    ├── Chart.yaml
    ├── values.yaml
    └── templates/
        ├── deployment.yaml
        └── service.yaml
```

---

## Chunk 1: Versions + ApplicationSet Fix + CNPG + Strimzi + ClickHouse

### Task 0: Update versions.yaml with Data Component Versions

**Files:**
- Modify: `versions.yaml`

- [ ] **Step 1: Add data section to versions.yaml**

  Replace the empty `data: {}` section with:

  ```yaml
  # Layer 2: Data platform (deployed by ArgoCD wave 4)
  data:
    postgresql: "17"               # CNPG cluster PG major version
    cnpg-cluster-instances: "3"    # Number of PG instances (HA)
    kafka-version: "3.9.0"         # Kafka version inside Strimzi
    kafka-replicas: "3"            # Kafka broker count
    clickhouse-version: "24.8"     # ClickHouse server version
    clickhouse-replicas: "1"       # ClickHouse replicas (dev profile)
    ferretdb: "1.24.0"             # FerretDB version
    otel-collector: "0.114.0"      # OpenTelemetry Collector version
    hyperdx: "1.7.0"               # HyperDX version
  ```

- [ ] **Step 2: Verify**

  ```bash
  python3 bootstrap/read_versions.py --section data --json
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add versions.yaml
  git commit -m "feat: add data platform versions to versions.yaml SSOT"
  ```

---

### Task 1: Fix layer2-data ApplicationSet Namespace Issue

**Files:**
- Modify: `argocd/appsets/layer2-data.yaml`

The current ApplicationSet has `namespace: "dfe-{{metadata.annotations...}}"` in the list elements. Annotation interpolation only works in the template block, not in list element values. Fix: use fixed namespaces per component (each data service owns its namespace).

- [ ] **Step 1: Update list elements with fixed namespaces**

  Replace the list elements in `argocd/appsets/layer2-data.yaml`:

  ```yaml
  elements:
    - app: cnpg-cluster
      namespace: cnpg
      wave: "4"
      project: data
    - app: clickhouse-cluster
      namespace: clickhouse
      wave: "4"
      project: data
    - app: strimzi-kafka
      namespace: strimzi
      wave: "4"
      project: data
    - app: ferretdb
      namespace: ferretdb
      wave: "4"
      project: data
    - app: otel-collector
      namespace: otel
      wave: "4"
      project: data
    - app: hyperdx
      namespace: hyperdx
      wave: "4"
      project: data
  ```

- [ ] **Step 2: Validate YAML**

  ```bash
  python3 -c "import yaml; list(yaml.safe_load_all(open('argocd/appsets/layer2-data.yaml')))" && echo "OK"
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add argocd/appsets/layer2-data.yaml
  git commit -m "fix: use fixed namespaces in layer2-data ApplicationSet (annotation interpolation fix)"
  ```

---

### Task 2: CNPG PostgreSQL Cluster Chart

**Files:**
- Create: `helm/charts/cnpg-cluster/Chart.yaml`
- Create: `helm/charts/cnpg-cluster/values.yaml`
- Create: `helm/charts/cnpg-cluster/templates/cluster.yaml`
- Create: `helm/charts/cnpg-cluster/templates/scheduled-backup.yaml`

CNPG operator (wave 3) creates the Cluster CRD kind. This chart templates a Cluster instance for DFE's shared PostgreSQL 17. Used by FerretDB, HyperDX, and dfe-engine.

- [ ] **Step 1: Create Chart.yaml**

  ```yaml
  apiVersion: v2
  name: cnpg-cluster
  description: CNPG PostgreSQL 17 cluster for DFE platform (FerretDB, HyperDX, dfe-engine)
  type: application
  version: 0.1.0
  appVersion: "17"
  dependencies:
    - name: dfe-common
      version: "0.1.0"
      repository: "file://../../library/dfe-common"
  ```

- [ ] **Step 2: Create values.yaml**

  ```yaml
  project: dfe
  component: cnpg-cluster
  env: local
  cloud: local

  cluster:
    name: dfe-pg
    instances: 3
    postgresqlVersion: "17"
    storage:
      size: 10Gi
      storageClass: ""  # empty = default StorageClass
    resources:
      requests:
        cpu: 500m
        memory: 1Gi
      limits:
        cpu: "2"
        memory: 4Gi
    # Databases to create
    databases:
      - dfe
      - ferretdb
      - hyperdx
    # Superuser secret (created by CNPG operator)
    superuserSecretName: dfe-pg-superuser

  backup:
    enabled: false  # Enable when S3/MinIO backup target is configured
    schedule: "0 0 */6 * * *"  # Every 6 hours
    retentionPolicy: "7d"
    destinationType: s3
    s3:
      endpoint: ""
      bucket: ""
      path: "/dfe/pg-backups"
  ```

- [ ] **Step 3: Create templates/cluster.yaml**

  ```yaml
  apiVersion: postgresql.cnpg.io/v1
  kind: Cluster
  metadata:
    name: {{ .Values.cluster.name }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    instances: {{ .Values.cluster.instances }}
    imageName: ghcr.io/cloudnative-pg/postgresql:{{ .Values.cluster.postgresqlVersion }}

    storage:
      size: {{ .Values.cluster.storage.size }}
      {{- if .Values.cluster.storage.storageClass }}
      storageClassName: {{ .Values.cluster.storage.storageClass }}
      {{- end }}

    resources:
      requests:
        cpu: {{ .Values.cluster.resources.requests.cpu }}
        memory: {{ .Values.cluster.resources.requests.memory }}
      limits:
        cpu: {{ .Values.cluster.resources.limits.cpu }}
        memory: {{ .Values.cluster.resources.limits.memory }}

    postgresql:
      parameters:
        max_connections: "200"
        shared_buffers: "256MB"
        effective_cache_size: "1GB"
        work_mem: "16MB"

    bootstrap:
      initdb:
        database: {{ index .Values.cluster.databases 0 }}
        owner: dfe
        postInitSQL:
          {{- range $db := .Values.cluster.databases }}
          {{- if ne $db (index $.Values.cluster.databases 0) }}
          - CREATE DATABASE {{ $db }};
          {{- end }}
          {{- end }}

    monitoring:
      enablePodMonitor: false  # OTel handles monitoring, not Prometheus

    superuserSecret:
      name: {{ .Values.cluster.superuserSecretName }}
  ```

- [ ] **Step 4: Create templates/scheduled-backup.yaml**

  ```yaml
  {{- if .Values.backup.enabled }}
  apiVersion: postgresql.cnpg.io/v1
  kind: ScheduledBackup
  metadata:
    name: {{ .Values.cluster.name }}-backup
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    schedule: {{ .Values.backup.schedule | quote }}
    backupOwnerReference: self
    cluster:
      name: {{ .Values.cluster.name }}
    method: barmanObjectStore
    target: prefer-standby
  {{- end }}
  ```

- [ ] **Step 5: Lint**

  ```bash
  cd helm/charts/cnpg-cluster && helm dependency update && helm lint . && helm template test .
  ```

- [ ] **Step 6: Commit**

  ```bash
  git add helm/charts/cnpg-cluster/
  git commit -m "feat: add cnpg-cluster chart (CNPG PostgreSQL 17, 3 instances, shared for FerretDB+HyperDX)"
  ```

---

### Task 3: Strimzi Kafka Chart

**Files:**
- Create: `helm/charts/strimzi-kafka/Chart.yaml`
- Create: `helm/charts/strimzi-kafka/values.yaml`
- Create: `helm/charts/strimzi-kafka/templates/kafka.yaml`
- Create: `helm/charts/strimzi-kafka/templates/kafka-user.yaml`

Strimzi operator (wave 3) creates the Kafka CRD kind. This chart templates a Kafka cluster for DFE's message bus. KRaft mode (no ZooKeeper), SASL/SCRAM-SHA-512, zstd compression.

- [ ] **Step 1: Create Chart.yaml**

  ```yaml
  apiVersion: v2
  name: strimzi-kafka
  description: Strimzi Kafka cluster for DFE pipeline (KRaft, SASL/SCRAM, zstd)
  type: application
  version: 0.1.0
  appVersion: "3.9.0"
  dependencies:
    - name: dfe-common
      version: "0.1.0"
      repository: "file://../../library/dfe-common"
  ```

- [ ] **Step 2: Create values.yaml**

  ```yaml
  project: dfe
  component: kafka
  env: local
  cloud: local

  kafka:
    name: dfe-kafka
    version: "3.9.0"
    replicas: 3
    storage:
      type: persistent-claim
      size: 20Gi
      storageClass: ""
    resources:
      requests:
        cpu: 500m
        memory: 2Gi
      limits:
        cpu: "2"
        memory: 4Gi
    config:
      compressionType: zstd
      logRetentionHours: "72"
      numPartitions: "6"
      defaultReplicationFactor: "3"
      minInsyncReplicas: "2"

  # DFE service user (SCRAM-SHA-512)
  user:
    name: dfe-kafka-user
    authentication:
      type: scram-sha-512
  ```

- [ ] **Step 3: Create templates/kafka.yaml**

  ```yaml
  apiVersion: kafka.strimzi.io/v1beta2
  kind: Kafka
  metadata:
    name: {{ .Values.kafka.name }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    kafka:
      version: {{ .Values.kafka.version }}
      replicas: {{ .Values.kafka.replicas }}

      listeners:
        - name: plain
          port: 9092
          type: internal
          tls: false
          authentication:
            type: scram-sha-512
        - name: tls
          port: 9093
          type: internal
          tls: true
          authentication:
            type: scram-sha-512

      config:
        offsets.topic.replication.factor: {{ .Values.kafka.config.defaultReplicationFactor }}
        transaction.state.log.replication.factor: {{ .Values.kafka.config.defaultReplicationFactor }}
        transaction.state.log.min.isr: {{ .Values.kafka.config.minInsyncReplicas }}
        default.replication.factor: {{ .Values.kafka.config.defaultReplicationFactor }}
        min.insync.replicas: {{ .Values.kafka.config.minInsyncReplicas }}
        num.partitions: {{ .Values.kafka.config.numPartitions }}
        log.retention.hours: {{ .Values.kafka.config.logRetentionHours }}
        compression.type: {{ .Values.kafka.config.compressionType }}

      storage:
        type: {{ .Values.kafka.storage.type }}
        size: {{ .Values.kafka.storage.size }}
        {{- if .Values.kafka.storage.storageClass }}
        class: {{ .Values.kafka.storage.storageClass }}
        {{- end }}

      resources:
        requests:
          cpu: {{ .Values.kafka.resources.requests.cpu }}
          memory: {{ .Values.kafka.resources.requests.memory }}
        limits:
          cpu: {{ .Values.kafka.resources.limits.cpu }}
          memory: {{ .Values.kafka.resources.limits.memory }}

    # KRaft mode — no ZooKeeper
    entityOperator:
      topicOperator: {}
      userOperator: {}
  ```

- [ ] **Step 4: Create templates/kafka-user.yaml**

  ```yaml
  apiVersion: kafka.strimzi.io/v1beta2
  kind: KafkaUser
  metadata:
    name: {{ .Values.user.name }}
    labels:
      strimzi.io/cluster: {{ .Values.kafka.name }}
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    authentication:
      type: {{ .Values.user.authentication.type }}
    authorization:
      type: simple
      acls:
        # Allow DFE services to produce/consume on all dfe topics
        - resource:
            type: topic
            name: "*"
            patternType: literal
          operations: ["Read", "Write", "Create", "Describe"]
        - resource:
            type: group
            name: "dfe-*"
            patternType: prefix
          operations: ["Read", "Describe"]
  ```

- [ ] **Step 5: Lint**

  ```bash
  cd helm/charts/strimzi-kafka && helm dependency update && helm lint . && helm template test .
  ```

- [ ] **Step 6: Commit**

  ```bash
  git add helm/charts/strimzi-kafka/
  git commit -m "feat: add strimzi-kafka chart (KRaft, 3 brokers, SASL/SCRAM, zstd, 72h retention)"
  ```

---

### Task 4: ClickHouse Cluster Chart

**Files:**
- Create: `helm/charts/clickhouse-cluster/Chart.yaml`
- Create: `helm/charts/clickhouse-cluster/values.yaml`
- Create: `helm/charts/clickhouse-cluster/templates/clickhouse.yaml`

Altinity ClickHouse operator (wave 3) creates the ClickHouseInstallation CRD. This chart templates a ClickHouse cluster for DFE analytics + observability.

- [ ] **Step 1: Create Chart.yaml**

  ```yaml
  apiVersion: v2
  name: clickhouse-cluster
  description: ClickHouse cluster for DFE analytics and observability (via Altinity operator)
  type: application
  version: 0.1.0
  appVersion: "24.8"
  dependencies:
    - name: dfe-common
      version: "0.1.0"
      repository: "file://../../library/dfe-common"
  ```

- [ ] **Step 2: Create values.yaml**

  ```yaml
  project: dfe
  component: clickhouse
  env: local
  cloud: local

  clickhouse:
    name: dfe-clickhouse
    version: "24.8"
    replicas: 1       # dev profile; 2-3 for production
    shardsCount: 1
    storage:
      size: 50Gi
      storageClass: ""
    resources:
      requests:
        cpu: 500m
        memory: 2Gi
      limits:
        cpu: "4"
        memory: 8Gi
    users:
      # Default admin user (password via ESO secret)
      admin:
        name: admin
        secretName: clickhouse-admin-password
      # DFE loader role (async insert, limited permissions)
      loader:
        name: dfe_loader_role
        databases: ["dfe"]
      # DFE query role (read-only for hunts)
      query:
        name: dfe_query_role
        databases: ["dfe"]
  ```

- [ ] **Step 3: Create templates/clickhouse.yaml**

  ```yaml
  apiVersion: "clickhouse.altinity.com/v1"
  kind: "ClickHouseInstallation"
  metadata:
    name: {{ .Values.clickhouse.name }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    defaults:
      templates:
        podTemplate: dfe-clickhouse-pod
        dataVolumeClaimTemplate: data
        serviceTemplate: dfe-clickhouse-svc
    configuration:
      clusters:
        - name: dfe
          layout:
            shardsCount: {{ .Values.clickhouse.shardsCount }}
            replicasCount: {{ .Values.clickhouse.replicas }}
      settings:
        # Async insert for dfe-loader (high throughput)
        async_insert: 1
        wait_for_async_insert: 0
        async_insert_max_data_size: 10485760  # 10MB
        async_insert_busy_timeout_ms: 5000
        max_execution_time: 600
      users:
        {{ .Values.clickhouse.users.loader.name }}/networks/ip: "::/0"
        {{ .Values.clickhouse.users.loader.name }}/profile: default
        {{ .Values.clickhouse.users.query.name }}/networks/ip: "::/0"
        {{ .Values.clickhouse.users.query.name }}/profile: readonly
    templates:
      podTemplates:
        - name: dfe-clickhouse-pod
          spec:
            containers:
              - name: clickhouse
                image: clickhouse/clickhouse-server:{{ .Values.clickhouse.version }}
                resources:
                  requests:
                    cpu: {{ .Values.clickhouse.resources.requests.cpu }}
                    memory: {{ .Values.clickhouse.resources.requests.memory }}
                  limits:
                    cpu: {{ .Values.clickhouse.resources.limits.cpu }}
                    memory: {{ .Values.clickhouse.resources.limits.memory }}
      volumeClaimTemplates:
        - name: data
          spec:
            accessModes: ["ReadWriteOnce"]
            resources:
              requests:
                storage: {{ .Values.clickhouse.storage.size }}
            {{- if .Values.clickhouse.storage.storageClass }}
            storageClassName: {{ .Values.clickhouse.storage.storageClass }}
            {{- end }}
      serviceTemplates:
        - name: dfe-clickhouse-svc
          spec:
            ports:
              - name: http
                port: 8123
              - name: native
                port: 9000
            type: ClusterIP
  ```

- [ ] **Step 4: Lint**

  ```bash
  cd helm/charts/clickhouse-cluster && helm dependency update && helm lint . && helm template test .
  ```

- [ ] **Step 5: Commit**

  ```bash
  git add helm/charts/clickhouse-cluster/
  git commit -m "feat: add clickhouse-cluster chart (Altinity operator CRD, async insert, tiered roles)"
  ```

---

## Chunk 2: FerretDB + OTel Collector + HyperDX + Smoke Test

### Task 5: FerretDB Chart

**Files:**
- Create: `helm/charts/ferretdb/Chart.yaml`
- Create: `helm/charts/ferretdb/values.yaml`
- Create: `helm/charts/ferretdb/templates/deployment.yaml`
- Create: `helm/charts/ferretdb/templates/service.yaml`

FerretDB provides MongoDB wire protocol over the CNPG PG17 cluster. Used by HyperDX for metadata storage. No operator — just a Deployment + Service.

- [ ] **Step 1: Create Chart.yaml**

  ```yaml
  apiVersion: v2
  name: ferretdb
  description: FerretDB — MongoDB wire protocol proxy over CNPG PostgreSQL 17
  type: application
  version: 0.1.0
  appVersion: "1.24.0"
  dependencies:
    - name: dfe-common
      version: "0.1.0"
      repository: "file://../../library/dfe-common"
  ```

- [ ] **Step 2: Create values.yaml**

  ```yaml
  project: dfe
  component: ferretdb
  env: local
  cloud: local

  image:
    repository: ghcr.io/ferretdb/ferretdb
    tag: ""  # defaults to Chart.appVersion
    pullPolicy: IfNotPresent

  replicaCount: 1

  postgresql:
    # Connection to CNPG cluster (created by cnpg-cluster chart)
    host: dfe-pg-rw.cnpg.svc.cluster.local
    port: 5432
    database: ferretdb
    # Credentials from CNPG-generated secret
    secretName: dfe-pg-app
    secretUserKey: username
    secretPasswordKey: password

  service:
    port: 27017
    type: ClusterIP

  resources:
    requests:
      cpu: 100m
      memory: 128Mi
    limits:
      cpu: 500m
      memory: 512Mi
  ```

- [ ] **Step 3: Create templates/deployment.yaml**

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
        containers:
          - name: ferretdb
            image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
            imagePullPolicy: {{ .Values.image.pullPolicy }}
            ports:
              - name: mongodb
                containerPort: 27017
            env:
              - name: FERRETDB_POSTGRESQL_URL
                value: "postgres://$(PGUSER):$(PGPASSWORD)@{{ .Values.postgresql.host }}:{{ .Values.postgresql.port }}/{{ .Values.postgresql.database }}?sslmode=prefer"
              - name: PGUSER
                valueFrom:
                  secretKeyRef:
                    name: {{ .Values.postgresql.secretName }}
                    key: {{ .Values.postgresql.secretUserKey }}
              - name: PGPASSWORD
                valueFrom:
                  secretKeyRef:
                    name: {{ .Values.postgresql.secretName }}
                    key: {{ .Values.postgresql.secretPasswordKey }}
            resources:
              {{- toYaml .Values.resources | nindent 14 }}
            livenessProbe:
              tcpSocket:
                port: mongodb
              initialDelaySeconds: 10
              periodSeconds: 10
            readinessProbe:
              tcpSocket:
                port: mongodb
              initialDelaySeconds: 5
              periodSeconds: 5
  ```

- [ ] **Step 4: Create templates/service.yaml**

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
        targetPort: mongodb
        protocol: TCP
        name: mongodb
    selector:
      {{- include "dfe-common.selectorLabels" . | nindent 4 }}
  ```

- [ ] **Step 5: Lint**

  ```bash
  cd helm/charts/ferretdb && helm dependency update && helm lint . && helm template test .
  ```

- [ ] **Step 6: Commit**

  ```bash
  git add helm/charts/ferretdb/
  git commit -m "feat: add ferretdb chart (MongoDB wire protocol over CNPG PG17)"
  ```

---

### Task 6: OTel Collector Chart

**Files:**
- Create: `helm/charts/otel-collector/Chart.yaml`
- Create: `helm/charts/otel-collector/values.yaml`
- Create: `helm/charts/otel-collector/templates/configmap.yaml`
- Create: `helm/charts/otel-collector/templates/daemonset.yaml`
- Create: `helm/charts/otel-collector/templates/gateway-deployment.yaml`
- Create: `helm/charts/otel-collector/templates/gateway-service.yaml`

Two-tier OTel Collector deployment per spec Section 2.5:
- **DaemonSet**: 1 pod per node, collects kubelet metrics + container logs + node metrics, forwards to Gateway
- **Gateway**: Deployment (1+ pods), receives from DaemonSet + DFE services, exports to ClickHouse + HyperDX, exposes Prometheus endpoint (:8889) for KEDA

- [ ] **Step 1: Create Chart.yaml**

  ```yaml
  apiVersion: v2
  name: otel-collector
  description: OpenTelemetry Collector — DaemonSet + Gateway topology for DFE observability
  type: application
  version: 0.1.0
  appVersion: "0.114.0"
  dependencies:
    - name: dfe-common
      version: "0.1.0"
      repository: "file://../../library/dfe-common"
  ```

- [ ] **Step 2: Create values.yaml**

  ```yaml
  project: dfe
  component: otel-collector
  env: local
  cloud: local

  image:
    repository: otel/opentelemetry-collector-contrib
    tag: ""  # defaults to Chart.appVersion
    pullPolicy: IfNotPresent

  gateway:
    replicas: 1
    resources:
      requests:
        cpu: 200m
        memory: 512Mi
      limits:
        cpu: "1"
        memory: 2Gi
    service:
      grpcPort: 4317
      httpPort: 4318
      prometheusPort: 8889

  daemonset:
    resources:
      requests:
        cpu: 100m
        memory: 256Mi
      limits:
        cpu: 500m
        memory: 512Mi

  exporters:
    # Path A: Direct to HyperDX
    hyperdx:
      enabled: true
      endpoint: "http://dfe-hyperdx.hyperdx.svc.cluster.local:4318"
    # ClickHouse exporter for raw telemetry
    clickhouse:
      enabled: true
      endpoint: "tcp://dfe-clickhouse.clickhouse.svc.cluster.local:9000"
      database: dfe
  ```

- [ ] **Step 3: Create templates/configmap.yaml**

  OTel Collector configuration for both DaemonSet and Gateway. Single ConfigMap with two configs selected by container args.

  ```yaml
  apiVersion: v1
  kind: ConfigMap
  metadata:
    name: {{ include "dfe-common.fullname" . }}-config
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  data:
    gateway-config.yaml: |
      receivers:
        otlp:
          protocols:
            grpc:
              endpoint: 0.0.0.0:4317
            http:
              endpoint: 0.0.0.0:4318

      processors:
        batch:
          timeout: 5s
          send_batch_size: 1024
        memory_limiter:
          check_interval: 5s
          limit_mib: 1536

      exporters:
        {{- if .Values.exporters.hyperdx.enabled }}
        otlphttp/hyperdx:
          endpoint: {{ .Values.exporters.hyperdx.endpoint }}
          tls:
            insecure: true
        {{- end }}
        prometheus:
          endpoint: 0.0.0.0:{{ .Values.gateway.service.prometheusPort }}
          metric_expiration: 5m
        debug:
          verbosity: basic

      service:
        pipelines:
          traces:
            receivers: [otlp]
            processors: [memory_limiter, batch]
            exporters:
              - debug
              {{- if .Values.exporters.hyperdx.enabled }}
              - otlphttp/hyperdx
              {{- end }}
          metrics:
            receivers: [otlp]
            processors: [memory_limiter, batch]
            exporters:
              - prometheus
              {{- if .Values.exporters.hyperdx.enabled }}
              - otlphttp/hyperdx
              {{- end }}
          logs:
            receivers: [otlp]
            processors: [memory_limiter, batch]
            exporters:
              - debug
              {{- if .Values.exporters.hyperdx.enabled }}
              - otlphttp/hyperdx
              {{- end }}

    daemonset-config.yaml: |
      receivers:
        filelog:
          include: [/var/log/pods/*/*/*.log]
          exclude: [/var/log/pods/*/otc-container/*.log]
          start_at: beginning
          include_file_path: true
          include_file_name: false
          operators:
            - type: container
              id: container-parser
        hostmetrics:
          collection_interval: 30s
          scrapers:
            cpu: {}
            memory: {}
            disk: {}
            filesystem: {}
            network: {}

      processors:
        batch:
          timeout: 5s
        k8sattributes:
          auth_type: serviceAccount
          extract:
            metadata: [k8s.namespace.name, k8s.pod.name, k8s.node.name]

      exporters:
        otlp/gateway:
          endpoint: {{ include "dfe-common.fullname" . }}-gateway.{{ .Release.Namespace }}.svc.cluster.local:4317
          tls:
            insecure: true

      service:
        pipelines:
          logs:
            receivers: [filelog]
            processors: [k8sattributes, batch]
            exporters: [otlp/gateway]
          metrics:
            receivers: [hostmetrics]
            processors: [k8sattributes, batch]
            exporters: [otlp/gateway]
  ```

- [ ] **Step 4: Create templates/daemonset.yaml**

  ```yaml
  apiVersion: apps/v1
  kind: DaemonSet
  metadata:
    name: {{ include "dfe-common.fullname" . }}-daemonset
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    selector:
      matchLabels:
        app.kubernetes.io/name: {{ include "dfe-common.fullname" . }}-daemonset
    template:
      metadata:
        labels:
          app.kubernetes.io/name: {{ include "dfe-common.fullname" . }}-daemonset
      spec:
        serviceAccountName: {{ include "dfe-common.fullname" . }}
        containers:
          - name: otel-collector
            image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
            args: ["--config=/etc/otel/daemonset-config.yaml"]
            resources:
              {{- toYaml .Values.daemonset.resources | nindent 14 }}
            volumeMounts:
              - name: config
                mountPath: /etc/otel
              - name: varlogpods
                mountPath: /var/log/pods
                readOnly: true
        volumes:
          - name: config
            configMap:
              name: {{ include "dfe-common.fullname" . }}-config
          - name: varlogpods
            hostPath:
              path: /var/log/pods
  ```

- [ ] **Step 5: Create templates/gateway-deployment.yaml**

  ```yaml
  apiVersion: apps/v1
  kind: Deployment
  metadata:
    name: {{ include "dfe-common.fullname" . }}-gateway
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    replicas: {{ .Values.gateway.replicas }}
    selector:
      matchLabels:
        app.kubernetes.io/name: {{ include "dfe-common.fullname" . }}-gateway
    template:
      metadata:
        labels:
          app.kubernetes.io/name: {{ include "dfe-common.fullname" . }}-gateway
      spec:
        containers:
          - name: otel-collector
            image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
            args: ["--config=/etc/otel/gateway-config.yaml"]
            ports:
              - name: otlp-grpc
                containerPort: 4317
              - name: otlp-http
                containerPort: 4318
              - name: prometheus
                containerPort: {{ .Values.gateway.service.prometheusPort }}
            resources:
              {{- toYaml .Values.gateway.resources | nindent 14 }}
            volumeMounts:
              - name: config
                mountPath: /etc/otel
        volumes:
          - name: config
            configMap:
              name: {{ include "dfe-common.fullname" . }}-config
  ```

- [ ] **Step 6: Create templates/gateway-service.yaml**

  ```yaml
  apiVersion: v1
  kind: Service
  metadata:
    name: {{ include "dfe-common.fullname" . }}-gateway
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    type: ClusterIP
    ports:
      - name: otlp-grpc
        port: {{ .Values.gateway.service.grpcPort }}
        targetPort: otlp-grpc
      - name: otlp-http
        port: {{ .Values.gateway.service.httpPort }}
        targetPort: otlp-http
      - name: prometheus
        port: {{ .Values.gateway.service.prometheusPort }}
        targetPort: prometheus
    selector:
      app.kubernetes.io/name: {{ include "dfe-common.fullname" . }}-gateway
  ```

- [ ] **Step 7: Lint**

  ```bash
  cd helm/charts/otel-collector && helm dependency update && helm lint . && helm template test .
  ```

- [ ] **Step 8: Commit**

  ```bash
  git add helm/charts/otel-collector/
  git commit -m "feat: add otel-collector chart (DaemonSet + Gateway, OTLP → HyperDX + Prometheus :8889)"
  ```

---

### Task 7: HyperDX Chart

**Files:**
- Create: `helm/charts/hyperdx/Chart.yaml`
- Create: `helm/charts/hyperdx/values.yaml`
- Create: `helm/charts/hyperdx/templates/deployment.yaml`
- Create: `helm/charts/hyperdx/templates/service.yaml`

HyperDX is the observability UI. Connects to ClickHouse for data and FerretDB (MongoDB protocol) for metadata.

- [ ] **Step 1: Create Chart.yaml**

  ```yaml
  apiVersion: v2
  name: hyperdx
  description: HyperDX observability UI — Kibana-like search + dashboards over ClickHouse
  type: application
  version: 0.1.0
  appVersion: "1.7.0"
  dependencies:
    - name: dfe-common
      version: "0.1.0"
      repository: "file://../../library/dfe-common"
  ```

- [ ] **Step 2: Create values.yaml**

  ```yaml
  project: dfe
  component: hyperdx
  env: local
  cloud: local

  image:
    repository: ghcr.io/hyperdx/hyperdx
    tag: ""  # defaults to Chart.appVersion
    pullPolicy: IfNotPresent

  replicaCount: 1

  # HyperDX connects to:
  clickhouse:
    host: dfe-clickhouse.clickhouse.svc.cluster.local
    port: 8123
    database: dfe
    user: admin
    secretName: clickhouse-admin-password
    secretKey: password

  # MongoDB-compatible metadata store (FerretDB over CNPG PG17)
  mongodb:
    uri: "mongodb://dfe-ferretdb.ferretdb.svc.cluster.local:27017/hyperdx"

  # OTLP ingest endpoint (receives telemetry from OTel Collector Path A)
  otlp:
    enabled: true
    grpcPort: 4317
    httpPort: 4318

  service:
    type: ClusterIP
    port: 8080

  resources:
    requests:
      cpu: 200m
      memory: 512Mi
    limits:
      cpu: "1"
      memory: 2Gi
  ```

- [ ] **Step 3: Create templates/deployment.yaml**

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
        containers:
          - name: hyperdx
            image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
            imagePullPolicy: {{ .Values.image.pullPolicy }}
            ports:
              - name: http
                containerPort: 8080
              {{- if .Values.otlp.enabled }}
              - name: otlp-grpc
                containerPort: {{ .Values.otlp.grpcPort }}
              - name: otlp-http
                containerPort: {{ .Values.otlp.httpPort }}
              {{- end }}
            env:
              - name: CLICKHOUSE_HOST
                value: {{ .Values.clickhouse.host }}
              - name: CLICKHOUSE_PORT
                value: {{ .Values.clickhouse.port | quote }}
              - name: CLICKHOUSE_DB
                value: {{ .Values.clickhouse.database }}
              - name: CLICKHOUSE_USER
                value: {{ .Values.clickhouse.user }}
              - name: CLICKHOUSE_PASSWORD
                valueFrom:
                  secretKeyRef:
                    name: {{ .Values.clickhouse.secretName }}
                    key: {{ .Values.clickhouse.secretKey }}
              - name: MONGO_URI
                value: {{ .Values.mongodb.uri }}
              - name: OTEL_EXPORTER_OTLP_ENDPOINT
                value: "http://dfe-otel-collector-gateway.otel.svc.cluster.local:4318"
            resources:
              {{- toYaml .Values.resources | nindent 14 }}
            livenessProbe:
              httpGet:
                path: /health/live
                port: http
              initialDelaySeconds: 15
              periodSeconds: 10
            readinessProbe:
              httpGet:
                path: /health/ready
                port: http
              initialDelaySeconds: 10
              periodSeconds: 5
  ```

- [ ] **Step 4: Create templates/service.yaml**

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
      - name: http
        port: {{ .Values.service.port }}
        targetPort: http
      {{- if .Values.otlp.enabled }}
      - name: otlp-grpc
        port: {{ .Values.otlp.grpcPort }}
        targetPort: otlp-grpc
      - name: otlp-http
        port: {{ .Values.otlp.httpPort }}
        targetPort: otlp-http
      {{- end }}
    selector:
      {{- include "dfe-common.selectorLabels" . | nindent 4 }}
  ```

- [ ] **Step 5: Lint**

  ```bash
  cd helm/charts/hyperdx && helm dependency update && helm lint . && helm template test .
  ```

- [ ] **Step 6: Commit**

  ```bash
  git add helm/charts/hyperdx/
  git commit -m "feat: add hyperdx chart (observability UI over ClickHouse + FerretDB)"
  ```

---

### Task 8: Layer 2 Data Smoke Test

**Files:**
- Create: `bootstrap/smoke-test-data.sh`

Quick verification that all wave 4 data components are healthy.

- [ ] **Step 1: Create `bootstrap/smoke-test-data.sh`**

  ```bash
  #!/usr/bin/env bash
  #  Project:      dfe-infra
  #  File:         smoke-test-data.sh
  #  Purpose:      Verify Layer 2 data platform components are healthy
  #  Language:     Bash
  #
  #  License:      FSL-1.1-ALv2
  #  Copyright:    (c) 2026 HYPERI PTY LIMITED
  set -euo pipefail

  PASS=0
  FAIL=0

  check() {
      local name="${1}"
      local cmd="${2}"
      if eval "${cmd}" > /dev/null 2>&1; then
          echo "  [PASS] ${name}"
          (( PASS++ )) || true
      else
          echo "  [FAIL] ${name}"
          (( FAIL++ )) || true
      fi
  }

  echo "=== DFE Layer 2 Data Platform Smoke Test ==="
  echo ""

  echo "--- Namespaces ---"
  check "cnpg namespace" "kubectl get ns cnpg"
  check "strimzi namespace" "kubectl get ns strimzi"
  check "clickhouse namespace" "kubectl get ns clickhouse"
  check "ferretdb namespace" "kubectl get ns ferretdb"
  check "otel namespace" "kubectl get ns otel"
  check "hyperdx namespace" "kubectl get ns hyperdx"

  echo ""
  echo "--- CNPG PostgreSQL ---"
  check "CNPG cluster ready" "kubectl -n cnpg get cluster dfe-pg -o jsonpath='{.status.phase}' | grep -q 'Cluster in healthy state'"
  check "CNPG instances running" "kubectl -n cnpg get pods -l cnpg.io/cluster=dfe-pg --field-selector=status.phase=Running -o name | grep -c . | grep -qE '^[1-9]'"

  echo ""
  echo "--- Strimzi Kafka ---"
  check "Kafka cluster ready" "kubectl -n strimzi get kafka dfe-kafka -o jsonpath='{.status.conditions[?(@.type==\"Ready\")].status}' | grep -q True"
  check "Kafka brokers running" "kubectl -n strimzi get pods -l strimzi.io/name=dfe-kafka-kafka --field-selector=status.phase=Running -o name | grep -c . | grep -qE '^[1-9]'"

  echo ""
  echo "--- ClickHouse ---"
  check "ClickHouse pods running" "kubectl -n clickhouse get pods -l clickhouse.altinity.com/chi=dfe-clickhouse --field-selector=status.phase=Running -o name | grep -c . | grep -qE '^[1-9]'"

  echo ""
  echo "--- FerretDB ---"
  check "FerretDB deployment ready" "kubectl -n ferretdb get deploy dfe-ferretdb -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"

  echo ""
  echo "--- OTel Collector ---"
  check "OTel Gateway running" "kubectl -n otel get deploy dfe-otel-collector-gateway -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
  check "OTel DaemonSet running" "kubectl -n otel get daemonset dfe-otel-collector-daemonset -o jsonpath='{.status.numberReady}' | grep -qE '^[1-9]'"

  echo ""
  echo "--- HyperDX ---"
  check "HyperDX deployment ready" "kubectl -n hyperdx get deploy dfe-hyperdx -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"

  echo ""
  echo "=== Results: ${PASS} passed, ${FAIL} failed ==="

  if (( FAIL > 0 )); then
      echo "Layer 2 data platform is NOT healthy."
      exit 1
  else
      echo "Layer 2 data platform is healthy."
  fi
  ```

- [ ] **Step 2: Make executable, validate**

  ```bash
  chmod +x bootstrap/smoke-test-data.sh
  bash -n bootstrap/smoke-test-data.sh && echo "OK"
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add bootstrap/smoke-test-data.sh
  git commit -m "feat: add Layer 2 data platform smoke test"
  ```

---

## Completion Criteria

This plan is complete when:
- [ ] `versions.yaml` has populated `data:` section with all component versions
- [ ] `argocd/appsets/layer2-data.yaml` uses fixed namespaces (no annotation interpolation in list elements)
- [ ] `helm lint` passes for all 6 new charts: cnpg-cluster, strimzi-kafka, clickhouse-cluster, ferretdb, otel-collector, hyperdx
- [ ] `helm template` renders valid YAML for each chart
- [ ] `bash -n bootstrap/smoke-test-data.sh` passes
- [ ] All files committed and pushed to main

**Live deployment** (optional, after merge):
- [ ] ArgoCD syncs wave 4 ApplicationSet — all 6 Applications created
- [ ] `bash bootstrap/smoke-test-data.sh` → all checks pass
- [ ] ClickHouse reachable at `clickhouse:8123`, Kafka at `kafka:9092`, PG at `cnpg:5432`
- [ ] HyperDX UI accessible, OTel Gateway receiving telemetry

**Next plan:** `2026-03-30-dfe-infra-04-auth-ingress.md` — Envoy Gateway SecurityPolicy, jwt_authn filter chain, HTTPRoutes for all services, NetworkPolicies.
