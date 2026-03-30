# DFE Infra 05 — KEDA Autoscaling

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a KEDA ScaledObjects Helm chart that scales DFE services: dfe-receiver and dfe-loader via `dfe_scaling_pressure` (OTel/Prometheus), and dfe-transform-* via Kafka consumer lag (scale-to-zero capable).

**Architecture:** A single `helm/charts/keda-scalers/` chart deploys ScaledObject CRDs for each DFE service. Two trigger types: (1) Prometheus trigger querying the OTel Collector Gateway `:8889` for `dfe_scaling_pressure` (Option A fallback — proven, no extra deps), (2) Kafka trigger for transforms using Strimzi SASL/SCRAM. Kedify OTEL Scaler (Option B) is documented but deferred until validated.

**Tech Stack:** KEDA ScaledObject CRDs, Prometheus trigger, Kafka trigger, Helm 3

---

## File Structure

```
helm/charts/keda-scalers/
├── Chart.yaml
├── values.yaml
└── templates/
    ├── scaledobject-receiver.yaml     # dfe-receiver: scaling_pressure trigger
    ├── scaledobject-loader.yaml       # dfe-loader: scaling_pressure trigger
    ├── scaledobject-transform.yaml    # dfe-transform-*: Kafka consumer lag, scale-to-zero
    └── trigger-auth-kafka.yaml        # TriggerAuthentication for Strimzi SASL/SCRAM
```

---

## Chunk 1: KEDA Scalers Chart

### Task 1: Create keda-scalers Chart Structure

**Files:**
- Create: `helm/charts/keda-scalers/Chart.yaml`
- Create: `helm/charts/keda-scalers/values.yaml`

- [ ] **Step 1: Create Chart.yaml**

  ```yaml
  apiVersion: v2
  name: keda-scalers
  description: KEDA ScaledObjects for DFE services (OTel metrics + Kafka consumer lag)
  type: application
  version: 0.1.0
  appVersion: "1.0.0"
  dependencies:
    - name: dfe-common
      version: "0.1.0"
      repository: "file://../../library/dfe-common"
  ```

- [ ] **Step 2: Create values.yaml**

  ```yaml
  project: dfe
  component: keda-scalers
  env: local
  cloud: local

  # OTel Collector Gateway Prometheus endpoint (exposed at :8889)
  prometheus:
    serverAddress: "http://dfe-otel-collector-gateway.otel.svc.cluster.local:8889"

  # Kafka connection for transform ScaledObjects
  kafka:
    bootstrapServers: "dfe-kafka-kafka-bootstrap.strimzi.svc.cluster.local:9092"
    # TriggerAuthentication references a K8s secret with SASL credentials
    authSecretName: dfe-kafka-user
    saslType: scram_sha512

  # --- Service scalers ---

  receiver:
    enabled: true
    deploymentName: dfe-receiver
    namespace: ""   # set via ArgoCD values
    minReplicas: 1
    maxReplicas: 10
    cooldownPeriod: 120
    pollingInterval: 15
    scalingPressureThreshold: "0.7"

  loader:
    enabled: true
    deploymentName: dfe-loader
    namespace: ""
    minReplicas: 1
    maxReplicas: 10
    cooldownPeriod: 120
    pollingInterval: 15
    scalingPressureThreshold: "0.7"

  # Transform apps — scale-to-zero via Kafka consumer lag
  # Each entry creates a ScaledObject tied to a single Kafka topic
  transforms: []
  # Example:
  # - name: transform-wasm
  #   deploymentName: dfe-transform-wasm
  #   namespace: ""
  #   topic: "events_land"
  #   consumerGroup: "dfe-transform-wasm"
  #   lagThreshold: "10"
  #   activationLagThreshold: "1"     # activate from zero at 1+ message
  #   minReplicas: 0                  # scale to zero when idle
  #   maxReplicas: 10
  #   cooldownPeriod: 300             # 5 min idle → scale to 0
  #   pollingInterval: 15
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add helm/charts/keda-scalers/
  git commit -m "feat: add keda-scalers chart structure (values for receiver, loader, transforms)"
  ```

---

### Task 2: ScaledObject Templates — Receiver + Loader (Prometheus Trigger)

**Files:**
- Create: `helm/charts/keda-scalers/templates/scaledobject-receiver.yaml`
- Create: `helm/charts/keda-scalers/templates/scaledobject-loader.yaml`

Both use the Prometheus trigger querying `dfe_scaling_pressure` from the OTel Collector Gateway's Prometheus endpoint (`:8889`). This is Option A (fallback) — proven, works today.

- [ ] **Step 1: Create scaledobject-receiver.yaml**

  ```yaml
  {{- if .Values.receiver.enabled }}
  apiVersion: keda.sh/v1alpha1
  kind: ScaledObject
  metadata:
    name: {{ .Values.receiver.deploymentName }}-scaler
    namespace: {{ .Values.receiver.namespace | default .Release.Namespace }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    scaleTargetRef:
      name: {{ .Values.receiver.deploymentName }}
    minReplicaCount: {{ .Values.receiver.minReplicas }}
    maxReplicaCount: {{ .Values.receiver.maxReplicas }}
    cooldownPeriod: {{ .Values.receiver.cooldownPeriod }}
    pollingInterval: {{ .Values.receiver.pollingInterval }}
    triggers:
      - type: prometheus
        metadata:
          serverAddress: {{ .Values.prometheus.serverAddress }}
          metricName: dfe_scaling_pressure
          threshold: {{ .Values.receiver.scalingPressureThreshold | quote }}
          query: |
            avg(dfe_scaling_pressure{service_name="dfe-receiver"})
  {{- end }}
  ```

- [ ] **Step 2: Create scaledobject-loader.yaml**

  ```yaml
  {{- if .Values.loader.enabled }}
  apiVersion: keda.sh/v1alpha1
  kind: ScaledObject
  metadata:
    name: {{ .Values.loader.deploymentName }}-scaler
    namespace: {{ .Values.loader.namespace | default .Release.Namespace }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    scaleTargetRef:
      name: {{ .Values.loader.deploymentName }}
    minReplicaCount: {{ .Values.loader.minReplicas }}
    maxReplicaCount: {{ .Values.loader.maxReplicas }}
    cooldownPeriod: {{ .Values.loader.cooldownPeriod }}
    pollingInterval: {{ .Values.loader.pollingInterval }}
    triggers:
      - type: prometheus
        metadata:
          serverAddress: {{ .Values.prometheus.serverAddress }}
          metricName: dfe_scaling_pressure
          threshold: {{ .Values.loader.scalingPressureThreshold | quote }}
          query: |
            avg(dfe_scaling_pressure{service_name="dfe-loader"})
  {{- end }}
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add helm/charts/keda-scalers/templates/scaledobject-receiver.yaml \
          helm/charts/keda-scalers/templates/scaledobject-loader.yaml
  git commit -m "feat: add ScaledObjects for dfe-receiver + dfe-loader (Prometheus dfe_scaling_pressure)"
  ```

---

### Task 3: ScaledObject Template — Transforms (Kafka Scale-to-Zero)

**Files:**
- Create: `helm/charts/keda-scalers/templates/scaledobject-transform.yaml`
- Create: `helm/charts/keda-scalers/templates/trigger-auth-kafka.yaml`

Each transform entry in `values.transforms[]` generates a ScaledObject with a Kafka trigger. `minReplicaCount: 0` enables scale-to-zero.

- [ ] **Step 1: Create trigger-auth-kafka.yaml**

  ```yaml
  {{- if .Values.transforms }}
  apiVersion: keda.sh/v1alpha1
  kind: TriggerAuthentication
  metadata:
    name: dfe-kafka-auth
    namespace: {{ .Release.Namespace }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    secretTargetRef:
      - parameter: sasl
        name: {{ .Values.kafka.authSecretName }}
        key: sasl.jaas.config
      - parameter: username
        name: {{ .Values.kafka.authSecretName }}
        key: user
      - parameter: password
        name: {{ .Values.kafka.authSecretName }}
        key: password
  {{- end }}
  ```

- [ ] **Step 2: Create scaledobject-transform.yaml**

  ```yaml
  {{- range .Values.transforms }}
  ---
  apiVersion: keda.sh/v1alpha1
  kind: ScaledObject
  metadata:
    name: {{ .deploymentName }}-scaler
    namespace: {{ .namespace | default $.Release.Namespace }}
    labels:
      {{- include "dfe-common.labels" $ | nindent 4 }}
  spec:
    scaleTargetRef:
      name: {{ .deploymentName }}
    minReplicaCount: {{ .minReplicas | default 0 }}
    maxReplicaCount: {{ .maxReplicas | default 10 }}
    cooldownPeriod: {{ .cooldownPeriod | default 300 }}
    pollingInterval: {{ .pollingInterval | default 15 }}
    triggers:
      - type: kafka
        authenticationRef:
          name: dfe-kafka-auth
        metadata:
          bootstrapServers: {{ $.Values.kafka.bootstrapServers }}
          consumerGroup: {{ .consumerGroup }}
          topic: {{ .topic }}
          lagThreshold: {{ .lagThreshold | default "10" | quote }}
          activationLagThreshold: {{ .activationLagThreshold | default "1" | quote }}
          saslType: {{ $.Values.kafka.saslType }}
          tls: "disable"
  {{- end }}
  ```

- [ ] **Step 3: Lint and verify**

  ```bash
  cd helm/charts/keda-scalers && helm dependency update && helm lint .
  # Default: should render receiver + loader ScaledObjects only (transforms empty)
  helm template test .

  # With a transform:
  helm template test . --set 'transforms[0].name=wasm' \
    --set 'transforms[0].deploymentName=dfe-transform-wasm' \
    --set 'transforms[0].topic=events_land' \
    --set 'transforms[0].consumerGroup=dfe-transform-wasm' | grep "ScaledObject"
  ```

  Expected: 3 ScaledObjects (receiver, loader, transform-wasm) + 1 TriggerAuthentication.

- [ ] **Step 4: Commit**

  ```bash
  git add helm/charts/keda-scalers/templates/
  git commit -m "feat: add transform ScaledObjects (Kafka scale-to-zero) + TriggerAuthentication"
  ```

---

### Task 4: Add keda-scalers to ArgoCD + Smoke Test

**Files:**
- Create: `argocd/bootstrap/keda-scalers-app.yaml`
- Modify: `bootstrap/bootstrap.sh`
- Create: `bootstrap/smoke-test-keda.sh`

- [ ] **Step 1: Create standalone ArgoCD Application**

  ```yaml
  # In-repo chart — standalone Application (same pattern as envoy-gateway-config)
  apiVersion: argoproj.io/v1alpha1
  kind: Application
  metadata:
    name: keda-scalers
    namespace: argocd
    annotations:
      argocd.argoproj.io/sync-wave: "5"
  spec:
    project: infra
    source:
      repoURL: https://github.com/catinspace-au/dfe-infra.git
      targetRevision: main
      path: helm/charts/keda-scalers
      helm:
        valueFiles:
          - ../../../argocd/values/common.yaml
          - ../../../argocd/values/local.yaml
    destination:
      server: https://kubernetes.default.svc
      namespace: keda
    syncPolicy:
      automated:
        prune: true
        selfHeal: true
      syncOptions:
        - CreateNamespace=true
        - ServerSideApply=true
  ```

- [ ] **Step 2: Add to bootstrap.sh**

  Add after the network-policies apply:
  ```bash
  run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/keda-scalers-app.yaml"
  ```

- [ ] **Step 3: Create smoke test**

  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  PASS=0; FAIL=0
  check() {
      local name="${1}" cmd="${2}"
      if eval "${cmd}" > /dev/null 2>&1; then
          echo "  [PASS] ${name}"; (( PASS++ )) || true
      else
          echo "  [FAIL] ${name}"; (( FAIL++ )) || true
      fi
  }
  echo "=== DFE KEDA Smoke Test ==="
  echo ""
  echo "--- KEDA Operator ---"
  check "KEDA operator running" "kubectl -n keda get deploy keda-operator -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
  check "KEDA metrics server" "kubectl -n keda get deploy keda-operator-metrics-apiserver -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
  echo ""
  echo "--- ScaledObjects ---"
  check "receiver ScaledObject exists" "kubectl get scaledobject dfe-receiver-scaler --all-namespaces -o name | grep -q scaledobject"
  check "loader ScaledObject exists" "kubectl get scaledobject dfe-loader-scaler --all-namespaces -o name | grep -q scaledobject"
  echo ""
  echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
  if (( FAIL > 0 )); then echo "KEDA is NOT healthy."; exit 1
  else echo "KEDA is healthy."; fi
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add argocd/bootstrap/keda-scalers-app.yaml bootstrap/bootstrap.sh bootstrap/smoke-test-keda.sh
  git commit -m "feat: add keda-scalers ArgoCD Application + KEDA smoke test"
  ```

---

## Completion Criteria

- [ ] `helm lint helm/charts/keda-scalers/` passes
- [ ] `helm template` renders receiver + loader ScaledObjects (Prometheus trigger)
- [ ] `helm template` with transforms renders Kafka ScaledObjects with `minReplicaCount: 0`
- [ ] `bash -n bootstrap/smoke-test-keda.sh` passes
- [ ] All committed and pushed to main

**Next plan:** 06 (DFE Service charts)
