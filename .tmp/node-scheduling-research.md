# DFE-Infra Node Scheduling Research Report

**Date:** 2026-03-31
**Status:** Complete Research (No Code Changes)
**Scope:** Understanding current node scheduling implementation and gaps

---

## Executive Summary

The dfe-infra codebase **has zero node scheduling logic** across all layers. There are no nodeSelector, tolerations, or affinity configurations anywhere in:
- Helm charts (all 20 DFE service and data charts)
- Library chart helpers (dfe-common)
- ArgoCD values or ApplicationSets
- Bootstrap scripts
- Terraform modules

The label `dfe.hyperi.io/workload=dfe` and taint `dfe.hyperi.io/workload=dfe:NoSchedule` are **referenced only in the cluster-secret annotation bridge**, but they are **never used** to actually schedule workloads onto dedicated nodes.

This is a **critical gap** for production deployments where DFE workloads need to run on dedicated node pools separate from infrastructure services.

---

## Detailed Findings

### 1. dfe-common Library Chart

**Location:** `/projects/dfe-infra/helm/library/dfe-common/`

**Current helpers:**
- `dfe-common.labels` — app.kubernetes.io/*, dfe.hyperi.io/{env,cloud}
- `dfe-common.selectorLabels` — app.kubernetes.io/name only
- `dfe-common.fullname` — resource naming
- `dfe-common.namespace` — namespace template
- `dfe-common.serviceAccountName` — service account naming

**Missing:** No helpers for nodeSelector, tolerations, affinity, or any node targeting.

**Files:**
- Chart.yaml
- templates/_labels.tpl
- templates/_names.tpl

---

### 2. Helm Charts — All 20 Service & Data Charts

**Scanned charts:**
```
DFE Services (7):
  dfe-receiver
  dfe-loader
  dfe-archiver
  dfe-fetcher
  dfe-engine
  dfe-ui
  dfe-transform-{wasm,vrl,vector,elastic,splack}

Data Components (8):
  cnpg-cluster
  clickhouse-cluster
  strimzi-kafka
  ferretdb
  otel-collector
  hyperdx
  envoy-gateway-config
  network-policies
  keda-scalers

Other:
  hyperdx
  otel-collector
```

**Finding:** NONE of the 20 values.yaml files contain:
- `nodeSelector`
- `tolerations`
- `affinity`
- `nodeName`

**Example — dfe-engine values.yaml:**
Contains project, component, image, replicaCount, service, config, clickhouse, postgresql, otel, auth, resources, serviceAccount — but NO nodeScheduling.

**Example — dfe-receiver values.yaml:**
Same pattern — resources, service ports, config, Kafka, otel — but NO nodeScheduling.

---

### 3. Deployment Templates

All deployment templates follow this pattern (no node targeting):

```yaml
spec:
  serviceAccountName: {{ include "dfe-common.serviceAccountName" . }}
  containers:
    - name: engine
      image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
      # ... environment, volumeMounts, resources, probes ...
  volumes:
    - name: config
      {{- if .Values.config.nfs.server }}
      nfs:
        server: {{ .Values.config.nfs.server }}
        path: {{ .Values.config.nfs.path }}
      {{- else }}
      emptyDir: {}
      {{- end }}
```

**Missing fields:**
- `nodeSelector`
- `tolerations`
- `affinity`
- `topologySpreadConstraints`

No template conditionals for node targeting.

---

### 4. ArgoCD Configuration

**values/common.yaml:** Global defaults for all clouds — no nodeScheduling block.

**values/local.yaml:** Local overrides — no nodeScheduling block.

**values/aws.yaml, gcp.yaml, azure.yaml:** Cloud-specific overrides — no nodeScheduling.

**appsets/layer2-apps.yaml, layer2-data.yaml:** ApplicationSets using matrix generator with cluster selector and list generator for apps — no code path to pass nodeScheduling values.

The ApplicationSets pass `common.yaml` and cloud-specific `.yaml` via `valueFiles`, but there is no mechanism to pass node targeting to the deployed Helm charts.

---

### 5. Bootstrap Process

**cluster-secret.yaml.tpl:** References `dfe.hyperi.io/*` annotations for identity, GitOps source, and infrastructure outputs, but:
- Does NOT include `dfe.hyperi.io/workload` label or taint
- Does NOT include node targeting metadata

**bootstrap.sh:** No kubectl label or taint commands. No node targeting logic.

---

### 6. Terraform Modules

Examined:
- tf-naming
- tf-iam
- tf-secrets
- tf-storage

**Finding:** No node group definitions, taints, or labels. tf-iam outputs workload identity annotations, but no node targeting.

---

### 7. Specification

**docs/superpowers/specs/2026-03-30-dfe-infra-design.md:**
- Section 2.1: Two-layer model (no node targeting strategy)
- Section 2.2: Deployment targets (K8s variants, storage, secrets, ingress — no node pools)
- Section 2.4: Ingress/Auth (OIDC — no affinity)
- Section 2.6: KEDA (metrics-driven scaling — no node affinity)

**No design section** for node pool targeting, taints, or affinity.

---

## What's Missing (Implementation Gaps)

### 1. **dfe-common Library Helper**

Add `_scheduling.tpl`:
```
{{- define "dfe-common.nodeScheduling" -}}
{{- if .Values.nodeScheduling }}
  nodeSelector:
    {{- toYaml .Values.nodeScheduling.nodeSelector | nindent 4 }}
  {{- if .Values.nodeScheduling.tolerations }}
  tolerations:
    {{- toYaml .Values.nodeScheduling.tolerations | nindent 4 }}
  {{- end }}
  {{- if .Values.nodeScheduling.affinity }}
  affinity:
    {{- toYaml .Values.nodeScheduling.affinity | nindent 4 }}
  {{- end }}
{{- end }}
{{- end }}
```

### 2. **values.yaml Schema** (All 20 charts)

Add standardized nodeScheduling block:
```yaml
nodeScheduling:
  nodeSelector: {}
  tolerations: []
  affinity: {}
```

### 3. **Deployment Templates** (All 20 charts)

Update pod spec to include:
```yaml
spec:
  {{- include "dfe-common.nodeScheduling" . | nindent 2 }}
  containers:
```

### 4. **ArgoCD values**

**common.yaml:**
```yaml
nodeScheduling:
  nodeSelector: {}
  tolerations: []
```

**local.yaml, aws.yaml, gcp.yaml, azure.yaml:** Cloud-specific overrides.

### 5. **Terraform**

Provision DFE node pools with:
- Label: `dfe.hyperi.io/workload=dfe`
- Taint: `dfe.hyperi.io/workload=dfe:NoSchedule`

Output node targeting info to cluster-secret annotations.

### 6. **Bootstrap Script**

Add optional label/taint application:
```bash
if [[ -n "${DFE_NODE_SELECTOR:-}" ]]; then
  kubectl label nodes -l "${DFE_NODE_SELECTOR}" \
    "dfe.hyperi.io/workload=dfe" --overwrite
  kubectl taint nodes -l "${DFE_NODE_SELECTOR}" \
    "dfe.hyperi.io/workload=dfe:NoSchedule" --overwrite
fi
```

---

## How Node Targeting Should Wire Through

```
┌─ Terraform ──────────────────────────────────────────────────────┐
│  Provision DFE node pool with:                                   │
│  - Labels: dfe.hyperi.io/workload=dfe                           │
│  - Taints: dfe.hyperi.io/workload=dfe:NoSchedule               │
│  - Output labels/taints to cluster metadata                      │
└──────────────┬────────────────────────────────────────────────────┘
               │
               ▼
┌─ Bootstrap Script ───────────────────────────────────────────────┐
│  1. Read Terraform outputs (node selector, taint info)           │
│  2. kubectl label/taint nodes if needed                          │
│  3. Pass node targeting to argocd/values via cluster-secret      │
└──────────────┬────────────────────────────────────────────────────┘
               │
               ▼
┌─ Cluster Secret Annotations ─────────────────────────────────────┐
│  dfe.hyperi.io/dfe_node_selector: "dfe.hyperi.io/workload=dfe"  │
│  dfe.hyperi.io/dfe_node_taints: "[{...}]"                       │
└──────────────┬────────────────────────────────────────────────────┘
               │
               ▼
┌─ ArgoCD ApplicationSets ─────────────────────────────────────────┐
│  Pass via {{ .metadata.annotations.dfe\.hyperi\.io/dfe_node_* }} │
│  into argocd/values/{cloud}.yaml                                 │
└──────────────┬────────────────────────────────────────────────────┘
               │
               ▼
┌─ ArgoCD values/{local,aws,gcp,azure}.yaml ──────────────────────┐
│  Populate nodeScheduling block based on annotation values        │
│  OR use hardcoded defaults per cloud                             │
└──────────────┬────────────────────────────────────────────────────┘
               │
               ▼
┌─ Helm charts (all 20) ───────────────────────────────────────────┐
│  Read .Values.nodeScheduling (from dfe-common library)           │
│  Render nodeSelector, tolerations, affinity in pod spec          │
│  Result: DFE pods land on dedicated DFE nodes                    │
└──────────────────────────────────────────────────────────────────┘
```

---

## Files Requiring Changes (Summary)

**Helm (22 files):**
- `/helm/library/dfe-common/templates/_scheduling.tpl` (NEW)
- `/helm/charts/*/values.yaml` (20 files — add nodeScheduling)
- `/helm/charts/*/templates/deployment.yaml` (20 files — add include)

**ArgoCD (5 files):**
- `argocd/values/common.yaml` (add defaults)
- `argocd/values/local.yaml` (cloud-specific)
- `argocd/values/aws.yaml` (cloud-specific)
- `argocd/values/gcp.yaml` (cloud-specific)
- `argocd/values/azure.yaml` (cloud-specific)

**Bootstrap (2 files):**
- `bootstrap/bootstrap.sh` (add node labeling/tainting)
- `bootstrap/templates/cluster-secret.yaml.tpl` (add node targeting metadata)

**Terraform (cloud-specific, not yet examined):**
- Node group provisioning modules
- Labeling and tainting logic
- Cluster-secret metadata outputs

---

## Conclusion

**Current Status:** Zero node scheduling implementation across the entire stack.

**Gap:** DFE workloads cannot be isolated to dedicated node pools. This is critical for production deployments.

**Solution Complexity:** Moderate — the infrastructure exists (Helm values cascade, ApplicationSets, bootstrap), but the node targeting feature must be added systematically across three layers: Helm templates, ArgoCD values, and Terraform.

**Priority:** Should be implemented before production deployments to Rancher local devex and before cloud migrations (AWS, GCP, Azure).

