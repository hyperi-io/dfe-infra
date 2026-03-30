# DFE Infra 04 — Auth & Ingress

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add HTTPRoutes for all DFE services, OIDC SecurityPolicy with jwt_authn claim forwarding, and NetworkPolicies — so that a single OIDC login works across dfe-ui, dfe-engine, ArgoCD, and HyperDX with role-based access control.

**Architecture:** Envoy Gateway (deployed in Plans 02-03) is the single entry point. This plan adds the routing and auth layer on top: HTTPRoutes direct traffic to backend services, SecurityPolicy handles OIDC login flows (conditional — off by default), and an EnvoyPatchPolicy chains a jwt_authn filter to extract claims into X-Forwarded-User and X-Auth-Groups headers. NetworkPolicies enforce namespace-level isolation per spec Section 2.9.

**Tech Stack:** Gateway API v1 (HTTPRoute, GatewayClass), Envoy Gateway SecurityPolicy, EnvoyPatchPolicy, Kubernetes NetworkPolicy, Helm 3

**Depends on:** Plan 02 (Envoy Gateway operator + GatewayClass/Gateway deployed), Plan 03 (data services running for backend routing targets)

---

## File Structure

```
helm/charts/envoy-gateway-config/
├── templates/
│   ├── gateway-class.yaml      # (existing)
│   ├── gateway.yaml            # (existing)
│   ├── cluster-issuer.yaml     # (existing)
│   ├── httproute-dfe-ui.yaml       # NEW: *.domain/  → dfe-ui
│   ├── httproute-dfe-engine.yaml   # NEW: *.domain/api/ → dfe-engine
│   ├── httproute-argocd.yaml       # NEW: argocd.domain/ → argocd-server
│   ├── httproute-hyperdx.yaml      # NEW: hyperdx.domain/ → hyperdx
│   ├── security-policy.yaml        # NEW: OIDC SecurityPolicy (conditional)
│   └── envoy-patch-policy.yaml     # NEW: jwt_authn filter for claim forwarding
└── values.yaml                 # (modify: add routes, oidc, jwt config)

helm/charts/network-policies/
├── Chart.yaml
├── values.yaml
└── templates/
    ├── dfe-namespace.yaml      # DFE app namespace policies
    ├── data-namespace.yaml     # Data platform namespace policies
    └── otel-egress.yaml        # Allow all → OTel Collector egress
```

---

## Chunk 1: HTTPRoutes + OIDC SecurityPolicy

### Task 1: Add HTTPRoutes to envoy-gateway-config

**Files:**
- Modify: `helm/charts/envoy-gateway-config/values.yaml`
- Create: `helm/charts/envoy-gateway-config/templates/httproute-dfe-ui.yaml`
- Create: `helm/charts/envoy-gateway-config/templates/httproute-dfe-engine.yaml`
- Create: `helm/charts/envoy-gateway-config/templates/httproute-argocd.yaml`
- Create: `helm/charts/envoy-gateway-config/templates/httproute-hyperdx.yaml`

- [ ] **Step 1: Add route config to values.yaml**

  Add this block to `helm/charts/envoy-gateway-config/values.yaml`:

  ```yaml
  # HTTPRoutes — map hostnames to backend services
  routes:
    dfeUi:
      enabled: true
      hostname: "dfe"           # becomes {hostname}.{domain}
      backendService: dfe-ui
      backendNamespace: ""      # set via ArgoCD values overlay
      backendPort: 3000
      pathPrefix: /

    dfeEngine:
      enabled: true
      hostname: "dfe"
      backendService: dfe-engine
      backendNamespace: ""
      backendPort: 8000
      pathPrefix: /api

    argocd:
      enabled: true
      hostname: "argocd"
      backendService: argocd-server
      backendNamespace: argocd
      backendPort: 443

    hyperdx:
      enabled: true
      hostname: "hyperdx"
      backendService: dfe-hyperdx
      backendNamespace: ""      # set in values overlay (deployed with dfe-ui wave 5)
      backendPort: 8080

  # OIDC — disabled by default (local auth is default)
  oidc:
    enabled: false
    provider: ""                # google | entra | cognito | keycloak | custom
    issuerUrl: ""               # e.g. https://accounts.google.com
    clientId: ""
    clientSecretName: ""        # K8s secret name (ESO-synced)
    clientSecretKey: "client-secret"
    scopes:
      - openid
      - email
      - profile
      - groups

  # jwt_authn filter — chains after OIDC to extract claims into headers
  jwtAuthn:
    enabled: false              # only when oidc.enabled
    issuer: ""                  # same as oidc.issuerUrl
    audiences: []               # e.g. ["dfe-client-id"]
    forwardHeaders:
      user: X-Forwarded-User   # claim: email or sub
      groups: X-Auth-Groups    # claim: groups (comma-separated)
  ```

- [ ] **Step 2: Create HTTPRoute templates**

  Each HTTPRoute follows the same pattern. Create 4 files:

  **`templates/httproute-dfe-ui.yaml`:**
  ```yaml
  {{- if .Values.routes.dfeUi.enabled }}
  apiVersion: gateway.networking.k8s.io/v1
  kind: HTTPRoute
  metadata:
    name: dfe-ui
    namespace: {{ .Values.routes.dfeUi.backendNamespace | default .Release.Namespace }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    parentRefs:
      - name: {{ .Values.gateway.name }}
        namespace: {{ .Values.gateway.namespace }}
    hostnames:
      - "{{ .Values.routes.dfeUi.hostname }}.{{ .Values.domain }}"
    rules:
      - matches:
          - path:
              type: PathPrefix
              value: {{ .Values.routes.dfeUi.pathPrefix }}
        backendRefs:
          - name: {{ .Values.routes.dfeUi.backendService }}
            port: {{ .Values.routes.dfeUi.backendPort }}
  {{- end }}
  ```

  **`templates/httproute-dfe-engine.yaml`:**
  ```yaml
  {{- if .Values.routes.dfeEngine.enabled }}
  apiVersion: gateway.networking.k8s.io/v1
  kind: HTTPRoute
  metadata:
    name: dfe-engine
    namespace: {{ .Values.routes.dfeEngine.backendNamespace | default .Release.Namespace }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    parentRefs:
      - name: {{ .Values.gateway.name }}
        namespace: {{ .Values.gateway.namespace }}
    hostnames:
      - "{{ .Values.routes.dfeEngine.hostname }}.{{ .Values.domain }}"
    rules:
      - matches:
          - path:
              type: PathPrefix
              value: {{ .Values.routes.dfeEngine.pathPrefix }}
        backendRefs:
          - name: {{ .Values.routes.dfeEngine.backendService }}
            port: {{ .Values.routes.dfeEngine.backendPort }}
  {{- end }}
  ```

  **`templates/httproute-argocd.yaml`:**
  ```yaml
  {{- if .Values.routes.argocd.enabled }}
  apiVersion: gateway.networking.k8s.io/v1
  kind: HTTPRoute
  metadata:
    name: argocd
    namespace: {{ .Values.routes.argocd.backendNamespace }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    parentRefs:
      - name: {{ .Values.gateway.name }}
        namespace: {{ .Values.gateway.namespace }}
    hostnames:
      - "{{ .Values.routes.argocd.hostname }}.{{ .Values.domain }}"
    rules:
      - backendRefs:
          - name: {{ .Values.routes.argocd.backendService }}
            port: {{ .Values.routes.argocd.backendPort }}
  {{- end }}
  ```

  **`templates/httproute-hyperdx.yaml`:**
  ```yaml
  {{- if .Values.routes.hyperdx.enabled }}
  apiVersion: gateway.networking.k8s.io/v1
  kind: HTTPRoute
  metadata:
    name: hyperdx
    namespace: {{ .Values.routes.hyperdx.backendNamespace | default .Release.Namespace }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    parentRefs:
      - name: {{ .Values.gateway.name }}
        namespace: {{ .Values.gateway.namespace }}
    hostnames:
      - "{{ .Values.routes.hyperdx.hostname }}.{{ .Values.domain }}"
    rules:
      - backendRefs:
          - name: {{ .Values.routes.hyperdx.backendService }}
            port: {{ .Values.routes.hyperdx.backendPort }}
  {{- end }}
  ```

- [ ] **Step 3: Lint and template**

  ```bash
  cd helm/charts/envoy-gateway-config
  helm dependency update
  helm lint .
  helm template test . --set domain=test.example.com
  ```

  Verify: 4 HTTPRoutes rendered, all with correct hostnames.

- [ ] **Step 4: Commit**

  ```bash
  git add helm/charts/envoy-gateway-config/
  git commit -m "feat: add HTTPRoutes for dfe-ui, dfe-engine, argocd, hyperdx"
  ```

---

### Task 2: Add OIDC SecurityPolicy

**Files:**
- Create: `helm/charts/envoy-gateway-config/templates/security-policy.yaml`

Envoy Gateway SecurityPolicy performs the OAuth2 authorization code flow and issues a session cookie. Only created when `oidc.enabled: true`. Parameterized for any OIDC provider.

- [ ] **Step 1: Create `templates/security-policy.yaml`**

  ```yaml
  {{- if .Values.oidc.enabled }}
  apiVersion: gateway.envoyproxy.io/v1alpha1
  kind: SecurityPolicy
  metadata:
    name: dfe-oidc
    namespace: {{ .Values.gateway.namespace }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    targetRefs:
      - group: gateway.networking.k8s.io
        kind: Gateway
        name: {{ .Values.gateway.name }}
    oidc:
      provider:
        issuer: {{ .Values.oidc.issuerUrl }}
      clientID: {{ .Values.oidc.clientId }}
      clientSecret:
        name: {{ .Values.oidc.clientSecretName }}
        namespace: {{ .Values.gateway.namespace }}
      scopes:
        {{- toYaml .Values.oidc.scopes | nindent 8 }}
      redirectURL: "https://dfe.{{ .Values.domain }}/oauth2/callback"
      logoutPath: "/oauth2/logout"
  {{- end }}
  ```

- [ ] **Step 2: Lint with OIDC disabled (default) and enabled**

  ```bash
  # Default (OIDC disabled) — SecurityPolicy should NOT render
  helm template test . --set domain=test.example.com | grep -c "SecurityPolicy" || echo "OK: not rendered"

  # OIDC enabled — SecurityPolicy should render
  helm template test . --set domain=test.example.com \
    --set oidc.enabled=true \
    --set oidc.issuerUrl=https://accounts.google.com \
    --set oidc.clientId=my-client \
    --set oidc.clientSecretName=oidc-client-secret | grep "SecurityPolicy" && echo "OK: rendered"
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add helm/charts/envoy-gateway-config/templates/security-policy.yaml
  git commit -m "feat: add OIDC SecurityPolicy for Envoy Gateway (conditional, any provider)"
  ```

---

### Task 3: Add EnvoyPatchPolicy for jwt_authn Claim Forwarding

**Files:**
- Create: `helm/charts/envoy-gateway-config/templates/envoy-patch-policy.yaml`

The jwt_authn filter chains after the OIDC filter to extract claims from the ID token and forward them as headers (X-Forwarded-User, X-Auth-Groups). Only created when `jwtAuthn.enabled: true`.

- [ ] **Step 1: Create `templates/envoy-patch-policy.yaml`**

  ```yaml
  {{- if .Values.jwtAuthn.enabled }}
  apiVersion: gateway.envoyproxy.io/v1alpha1
  kind: EnvoyPatchPolicy
  metadata:
    name: dfe-jwt-authn
    namespace: {{ .Values.gateway.namespace }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    targetRef:
      group: gateway.networking.k8s.io
      kind: Gateway
      name: {{ .Values.gateway.name }}
    type: JSONPatch
    jsonPatches:
      - type: "type.googleapis.com/envoy.config.listener.v3.Listener"
        name: "{{ .Values.gateway.namespace }}/{{ .Values.gateway.name }}/https"
        operation:
          op: add
          path: "/default_filter_chain/filters/0/typed_config/http_filters/0"
          value:
            name: "envoy.filters.http.jwt_authn"
            typed_config:
              "@type": "type.googleapis.com/envoy.extensions.filters.http.jwt_authn.v3.JwtAuthentication"
              providers:
                oidc_provider:
                  issuer: {{ .Values.jwtAuthn.issuer }}
                  {{- if .Values.jwtAuthn.audiences }}
                  audiences:
                    {{- toYaml .Values.jwtAuthn.audiences | nindent 20 }}
                  {{- end }}
                  forward: true
                  from_headers:
                    - name: "Authorization"
                      value_prefix: "Bearer "
                  remote_jwks:
                    http_uri:
                      uri: "{{ .Values.jwtAuthn.issuer }}/.well-known/jwks.json"
                      cluster: "oidc_jwks"
                      timeout: 5s
                    cache_duration: 600s
                  claim_to_headers:
                    - header_name: {{ .Values.jwtAuthn.forwardHeaders.user }}
                      claim_name: email
                    - header_name: {{ .Values.jwtAuthn.forwardHeaders.groups }}
                      claim_name: groups
              rules:
                match:
                  prefix: "/"
                requires:
                  provider_name: oidc_provider
  {{- end }}
  ```

- [ ] **Step 2: Lint and verify conditional rendering**

  ```bash
  # Default (disabled) — should not render
  helm template test . --set domain=test.example.com | grep -c "EnvoyPatchPolicy" || echo "OK: not rendered"

  # Enabled
  helm template test . --set domain=test.example.com \
    --set jwtAuthn.enabled=true \
    --set jwtAuthn.issuer=https://accounts.google.com | grep "EnvoyPatchPolicy" && echo "OK: rendered"
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add helm/charts/envoy-gateway-config/templates/envoy-patch-policy.yaml
  git commit -m "feat: add jwt_authn EnvoyPatchPolicy for OIDC claim forwarding (X-Forwarded-User, X-Auth-Groups)"
  ```

---

## Chunk 2: NetworkPolicies + Smoke Test

### Task 4: NetworkPolicies Chart

**Files:**
- Create: `helm/charts/network-policies/Chart.yaml`
- Create: `helm/charts/network-policies/values.yaml`
- Create: `helm/charts/network-policies/templates/dfe-namespace.yaml`
- Create: `helm/charts/network-policies/templates/data-namespace.yaml`
- Create: `helm/charts/network-policies/templates/otel-egress.yaml`

Per spec Section 2.9:
- `dfe-*` namespaces: allow ingress from Envoy Gateway + other `dfe-*` namespaces
- Data namespaces (strimzi, clickhouse, cnpg): allow ingress only from `dfe-*` namespaces
- All namespaces: allow egress to OTel Collector
- `dfe-*` namespaces: deny ingress from `default` and unrelated namespaces

- [ ] **Step 1: Create Chart.yaml**

  ```yaml
  apiVersion: v2
  name: network-policies
  description: Kubernetes NetworkPolicies for DFE namespace isolation
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
  component: network-policies
  env: local
  cloud: local

  # Namespaces that get DFE app policies (ingress from gateway + dfe-*)
  dfeNamespaces:
    - dfe-local   # changes per env: dfe-dev, dfe-prod, etc.

  # Data platform namespaces (ingress only from dfe-* namespaces)
  dataNamespaces:
    - cnpg
    - strimzi
    - clickhouse
    - ferretdb

  # Envoy Gateway namespace (source of ingress traffic)
  gatewayNamespace: envoy-gateway-system

  # OTel Collector namespace (all pods can send telemetry here)
  otelNamespace: otel
  otelPort: 4317
  ```

- [ ] **Step 3: Create templates/dfe-namespace.yaml**

  ```yaml
  {{- range .Values.dfeNamespaces }}
  ---
  apiVersion: networking.k8s.io/v1
  kind: NetworkPolicy
  metadata:
    name: dfe-ingress-policy
    namespace: {{ . }}
    labels:
      {{- include "dfe-common.labels" $ | nindent 4 }}
  spec:
    podSelector: {}
    policyTypes:
      - Ingress
    ingress:
      # Allow from Envoy Gateway
      - from:
          - namespaceSelector:
              matchLabels:
                kubernetes.io/metadata.name: {{ $.Values.gatewayNamespace }}
      # Allow from other DFE namespaces
      {{- range $.Values.dfeNamespaces }}
      - from:
          - namespaceSelector:
              matchLabels:
                kubernetes.io/metadata.name: {{ . }}
      {{- end }}
  {{- end }}
  ```

- [ ] **Step 4: Create templates/data-namespace.yaml**

  ```yaml
  {{- range .Values.dataNamespaces }}
  ---
  apiVersion: networking.k8s.io/v1
  kind: NetworkPolicy
  metadata:
    name: data-ingress-policy
    namespace: {{ . }}
    labels:
      {{- include "dfe-common.labels" $ | nindent 4 }}
  spec:
    podSelector: {}
    policyTypes:
      - Ingress
    ingress:
      # Allow from DFE app namespaces only
      {{- range $.Values.dfeNamespaces }}
      - from:
          - namespaceSelector:
              matchLabels:
                kubernetes.io/metadata.name: {{ . }}
      {{- end }}
  {{- end }}
  ```

- [ ] **Step 5: Create templates/otel-egress.yaml**

  ```yaml
  # All DFE and data namespaces can send telemetry to OTel Collector
  {{- $allNamespaces := concat .Values.dfeNamespaces .Values.dataNamespaces }}
  {{- range $allNamespaces }}
  ---
  apiVersion: networking.k8s.io/v1
  kind: NetworkPolicy
  metadata:
    name: allow-otel-egress
    namespace: {{ . }}
    labels:
      {{- include "dfe-common.labels" $ | nindent 4 }}
  spec:
    podSelector: {}
    policyTypes:
      - Egress
    egress:
      # Allow egress to OTel Collector
      - to:
          - namespaceSelector:
              matchLabels:
                kubernetes.io/metadata.name: {{ $.Values.otelNamespace }}
        ports:
          - port: {{ $.Values.otelPort }}
            protocol: TCP
      # Allow DNS resolution
      - to: []
        ports:
          - port: 53
            protocol: UDP
          - port: 53
            protocol: TCP
  {{- end }}
  ```

- [ ] **Step 6: Lint**

  ```bash
  cd helm/charts/network-policies
  helm dependency update
  helm lint .
  helm template test .
  ```

  Verify: policies rendered for each namespace.

- [ ] **Step 7: Add network-policies to layer1-addons or as standalone Application**

  Since network-policies is an in-repo chart (like envoy-gateway-config), create a standalone Application:

  Create `argocd/bootstrap/network-policies-app.yaml`:
  ```yaml
  apiVersion: argoproj.io/v1alpha1
  kind: Application
  metadata:
    name: network-policies
    namespace: argocd
    annotations:
      argocd.argoproj.io/sync-wave: "2"
  spec:
    project: infra
    source:
      repoURL: https://github.com/catinspace-au/dfe-infra.git
      targetRevision: main
      path: helm/charts/network-policies
      helm:
        valueFiles:
          - ../../../argocd/values/common.yaml
          - ../../../argocd/values/local.yaml
    destination:
      server: https://kubernetes.default.svc
      namespace: default
    syncPolicy:
      automated:
        prune: true
        selfHeal: true
      syncOptions:
        - ServerSideApply=true
  ```

  Add to bootstrap.sh (after the envoy-gateway-config apply):
  ```bash
  run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/network-policies-app.yaml"
  ```

- [ ] **Step 8: Commit**

  ```bash
  git add helm/charts/network-policies/ argocd/bootstrap/network-policies-app.yaml bootstrap/bootstrap.sh
  git commit -m "feat: add network-policies chart (DFE namespace isolation per spec Section 2.9)"
  ```

---

### Task 5: Update ArgoCD Values for OIDC Integration

**Files:**
- Modify: `argocd/values/common.yaml`

Add ArgoCD-specific configuration: disable Dex (Envoy Gateway handles OIDC), configure RBAC for DFE roles.

- [ ] **Step 1: Add ArgoCD OIDC config to common.yaml**

  Add to `argocd/values/common.yaml`:

  ```yaml
  # ArgoCD — Envoy Gateway handles OIDC, not Dex
  argocd:
    dex:
      enabled: false  # Envoy Gateway manages OIDC for ArgoCD
    rbac:
      # Default RBAC policy — deny all, explicit grants only
      defaultPolicy: "role:readonly"
      # DFE role mappings (group → ArgoCD role)
      # Populated by dfe-engine generate_rbac_csv() when OIDC is enabled
      policy: |
        p, role:dfe-admin, applications, *, */*, allow
        p, role:dfe-admin, clusters, *, *, allow
        p, role:dfe-admin, repositories, *, *, allow
        p, role:dfe-operator, applications, get, */*, allow
        p, role:dfe-operator, applications, sync, */*, allow
        p, role:dfe-viewer, applications, get, */*, allow
        g, dfe-admins, role:dfe-admin
        g, dfe-operators, role:dfe-operator
        g, dfe-viewers, role:dfe-viewer
  ```

- [ ] **Step 2: Validate YAML**

  ```bash
  python3 -c "import yaml; yaml.safe_load(open('argocd/values/common.yaml'))" && echo "OK"
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add argocd/values/common.yaml
  git commit -m "feat: add ArgoCD RBAC config (Dex disabled, Envoy OIDC, DFE role policies)"
  ```

---

### Task 6: Auth & Ingress Smoke Test

**Files:**
- Create: `bootstrap/smoke-test-auth.sh`

- [ ] **Step 1: Create smoke test**

  ```bash
  #!/usr/bin/env bash
  #  Project:      dfe-infra
  #  File:         smoke-test-auth.sh
  #  Purpose:      Verify auth and ingress components are healthy
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

  echo "=== DFE Auth & Ingress Smoke Test ==="
  echo ""

  echo "--- Gateway API ---"
  check "GatewayClass exists" "kubectl get gatewayclass dfe-envoy"
  check "Gateway exists" "kubectl -n envoy-gateway-system get gateway dfe-gateway"
  check "Gateway programmed" "kubectl -n envoy-gateway-system get gateway dfe-gateway -o jsonpath='{.status.conditions[?(@.type==\"Programmed\")].status}' | grep -q True"

  echo ""
  echo "--- HTTPRoutes ---"
  check "dfe-ui HTTPRoute" "kubectl get httproute dfe-ui --all-namespaces -o name | grep -q httproute"
  check "dfe-engine HTTPRoute" "kubectl get httproute dfe-engine --all-namespaces -o name | grep -q httproute"
  check "argocd HTTPRoute" "kubectl -n argocd get httproute argocd"
  check "hyperdx HTTPRoute" "kubectl get httproute hyperdx --all-namespaces -o name | grep -q httproute"

  echo ""
  echo "--- Network Policies ---"
  check "DFE namespace ingress policy" "kubectl get networkpolicy dfe-ingress-policy --all-namespaces -o name | grep -q networkpolicy"
  check "Data namespace ingress policy" "kubectl -n cnpg get networkpolicy data-ingress-policy"
  check "OTel egress policy" "kubectl get networkpolicy allow-otel-egress --all-namespaces -o name | grep -q networkpolicy"

  echo ""
  echo "--- OIDC (conditional) ---"
  if kubectl -n envoy-gateway-system get securitypolicy dfe-oidc > /dev/null 2>&1; then
      check "OIDC SecurityPolicy exists" "true"
      check "OIDC SecurityPolicy accepted" "kubectl -n envoy-gateway-system get securitypolicy dfe-oidc -o jsonpath='{.status.conditions[?(@.type==\"Accepted\")].status}' | grep -q True"
  else
      echo "  [SKIP] OIDC not enabled (oidc.enabled=false)"
  fi

  echo ""
  echo "=== Results: ${PASS} passed, ${FAIL} failed ==="

  if (( FAIL > 0 )); then
      echo "Auth & ingress is NOT healthy."
      exit 1
  else
      echo "Auth & ingress is healthy."
  fi
  ```

- [ ] **Step 2: Make executable, validate**

  ```bash
  chmod +x bootstrap/smoke-test-auth.sh
  bash -n bootstrap/smoke-test-auth.sh && echo "OK"
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add bootstrap/smoke-test-auth.sh
  git commit -m "feat: add auth & ingress smoke test"
  ```

---

## Completion Criteria

This plan is complete when:
- [ ] `helm lint helm/charts/envoy-gateway-config/` passes (with new templates)
- [ ] `helm template` renders 4 HTTPRoutes when domain is set
- [ ] `helm template` renders SecurityPolicy only when `oidc.enabled=true`
- [ ] `helm template` renders EnvoyPatchPolicy only when `jwtAuthn.enabled=true`
- [ ] `helm lint helm/charts/network-policies/` passes
- [ ] `helm template` renders NetworkPolicies for DFE + data namespaces
- [ ] ArgoCD RBAC config in common.yaml with DFE roles
- [ ] `bash -n bootstrap/smoke-test-auth.sh` passes
- [ ] All files committed and pushed to main

**Live deployment** (optional):
- [ ] HTTPRoutes resolve — `curl -k https://dfe.devex.hyperi.io/` returns dfe-ui
- [ ] `curl -k https://argocd.devex.hyperi.io/` returns ArgoCD UI
- [ ] NetworkPolicies enforced — pods in `default` namespace cannot reach DFE services
- [ ] OIDC login works (when enabled) across all 4 services with single session

**Next plan:** `2026-03-30-dfe-infra-05-keda.md` — KEDA + Kedify OTEL Scaler + ScaledObjects wired to `dfe_scaling_pressure`.
