# DFE Infra 01 — Repository Foundation

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the repository skeleton — directory structure, tf-naming module (canonical naming standard), Helm library chart for shared labels, ArgoCD ApplicationSet skeleton, idempotent bootstrap script, and CI workflows — such that `tofu test` and `helm lint` pass on every component.

**Architecture:** tf-naming is a pure-output Terraform module that derives all platform-specific resource names from four input dimensions. Every other Terraform module and Helm chart will import or inherit from it. ArgoCD ApplicationSets use a matrix generator that reads Terraform-written cluster secret annotations — the bridge that makes Layer 2 cloud-agnostic. The bootstrap script (`bootstrap.sh`) is idempotent using `helm upgrade --install` and `kubectl apply` throughout.

**Tech Stack:** OpenTofu ≥1.6 (terraform-compatible), Helm 3, ArgoCD 2.x, Kubernetes 1.28+, GitHub Actions, bats (bash test framework for bootstrap script), null provider ~3.0

---

## File Structure

```
terraform/modules/tf-naming/
├── main.tf                        # canonical locals + null_resource 30-char validation
├── variables.tf                   # project, component, env, cloud, region inputs + validations
├── outputs.tf                     # all derived names as module outputs
└── tests/
    └── naming.tftest.hcl          # OpenTofu native test: canonical, k8s, aws, gcp, az, vault names

helm/library/dfe-common/
├── Chart.yaml                     # type: library, version 0.1.0
└── templates/
    ├── _labels.tpl                # define "dfe-common.labels" macro (Section 9.4 K8s labels)
    └── _names.tpl                 # define "dfe-common.fullname" and "dfe-common.chart" helpers

helm/library/dfe-common/tests/
└── lint-test/
    ├── Chart.yaml                 # minimal chart that depends on dfe-common (for lint validation)
    └── templates/
        └── configmap.yaml        # uses {{- include "dfe-common.labels" . | nindent 4 }}

argocd/bootstrap/
├── appproject-bootstrap.yaml      # AppProjects: infra, data, dfe-apps
└── argocd-cluster-addons.yaml     # Root ApplicationSet (reads cluster secret annotations)

argocd/appsets/
├── layer1-addons.yaml             # Wave 2-3: cert-manager, ESO, Envoy Gateway, KEDA, operators
├── layer2-data.yaml               # Wave 4: Kafka, ClickHouse, CNPG, FerretDB, HyperDX, OTel
└── layer2-apps.yaml               # Wave 5: dfe-engine, dfe-ui, dfe-receiver, dfe-loader, etc.

argocd/values/
├── common.yaml                    # default values shared across all clouds
├── local.yaml                     # Rancher local overrides (externalIPs, local-path, OpenBao)
├── aws.yaml                       # AWS overrides (NLB annotations, EBS CSI, AWS SM)
├── gcp.yaml                       # GCP overrides (GCP LB, PD CSI, GCP SM)
└── azure.yaml                     # Azure overrides (Azure LB, Azure Disk CSI, Azure KV)

bootstrap/
├── bootstrap.sh                   # idempotent: cluster-secret → cert-manager → ESO → ArgoCD
└── templates/
    ├── cluster-secret.yaml.tpl    # cluster secret with all dfe.hyperi.io/* annotations
    ├── eso-cluster-secret-store.yaml.tpl  # ESO ClusterSecretStore pointing at secrets backend
    └── regcred.yaml.tpl           # imagePullSecret for JFrog container registry

bootstrap/tests/
└── bootstrap.bats                 # bats tests: idempotency, required vars, dry-run mode

.github/workflows/
├── tf-validate.yml                # tofu init + validate on every module; tofu test on tf-naming
├── helm-lint.yml                  # helm lint + helm template --dry-run on all charts
└── docker-build.yml               # build + push images to JFrog registry on merge to main

docker/
└── README.md                      # build contexts for images hosted in dfe-infra (utility images)

.gitignore                         # terraform state, .terraform/, *.pem, .env
DIRECTORY.md                       # canonical repo structure reference (onboarding doc)
docs/license-review.md             # OSS license audit tracking (policy ref: hyperi-io/licensing)
```

---

## Chunk 1: tf-naming Module (TDD)

### Task 1: Directory Scaffold + .gitignore

**Files:**
- Create: `.gitignore`
- Create directory tree (all empty dirs get `.gitkeep`)

- [ ] **Step 1: Create directory structure**

  ```bash
  mkdir -p terraform/modules/tf-naming/tests
  mkdir -p helm/library/dfe-common/templates
  mkdir -p helm/library/dfe-common/tests/lint-test/templates
  mkdir -p argocd/bootstrap argocd/appsets argocd/values
  mkdir -p bootstrap/templates bootstrap/tests
  mkdir -p .github/workflows
  touch helm/library/dfe-common/tests/lint-test/.gitkeep
  ```

- [ ] **Step 2: Create `.gitignore`**

  ```gitignore
  # Terraform / OpenTofu
  **/.terraform/
  **/.terraform.lock.hcl
  *.tfstate
  *.tfstate.backup
  *.tfplan
  override.tf
  override.tf.json
  *_override.tf
  *_override.tf.json

  # Helm
  **/charts/*.tgz

  # Secrets / certs
  *.pem
  *.key
  *.p12
  .env
  .env.*
  !.env.example

  # Editor / OS
  .DS_Store
  .vscode/
  .idea/
  *.swp
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add .gitignore
  git commit -m "chore: add .gitignore"
  git push
  ```

---

### Task 2: tf-naming — Write Failing Tests First

**Files:**
- Create: `terraform/modules/tf-naming/tests/naming.tftest.hcl`

- [ ] **Step 1: Write test file**

  ```hcl
  # terraform/modules/tf-naming/tests/naming.tftest.hcl

  # --- Happy path: standard dfe-loader-prod on AWS ---
  variables {
    project   = "dfe"
    component = "loader"
    env       = "prod"
    cloud     = "aws"
    region    = "us-east-1"
  }

  run "canonical_name_format" {
    command = plan

    assert {
      condition     = output.canonical_name == "dfe-loader-prod"
      error_message = "canonical_name must be {project}-{component}-{env}"
    }
  }

  run "k8s_names" {
    command = plan

    assert {
      condition     = output.k8s_namespace == "dfe-prod"
      error_message = "k8s_namespace must be {project}-{env}"
    }

    assert {
      condition     = output.k8s_service_account == "dfe-loader"
      error_message = "k8s_service_account must be {project}-{component}"
    }
  }

  run "rancher_names" {
    command = plan

    assert {
      condition     = output.rancher_cluster_name == "dfe-aws-prod"
      error_message = "rancher_cluster_name must be {project}-{cloud}-{env}"
    }

    assert {
      condition     = output.rancher_project_name == "dfe-prod"
      error_message = "rancher_project_name must be {project}-{env}"
    }
  }

  run "aws_names" {
    command = plan

    assert {
      condition     = output.aws_irsa_role_name == "dfe-loader-prod-irsa"
      error_message = "aws_irsa_role_name must be {canonical_name}-irsa"
    }
  }

  run "gcp_names" {
    command = plan

    assert {
      condition     = output.gcp_sa_account_id == "dfe-loader-prod"
      error_message = "gcp_sa_account_id must equal canonical_name (≤30 chars for GCP)"
    }
  }

  run "azure_names" {
    command = plan

    assert {
      condition     = output.azure_identity_name == "id-dfe-loader-prod"
      error_message = "azure_identity_name must be id-{canonical_name}"
    }

    assert {
      condition     = output.azure_federated_cred_name == "fc-dfe-loader-prod"
      error_message = "azure_federated_cred_name must be fc-{canonical_name}"
    }
  }

  run "vault_names" {
    command = plan

    assert {
      condition     = output.vault_approle_name == "dfe-loader-prod"
      error_message = "vault_approle_name must equal canonical_name"
    }

    assert {
      condition     = output.vault_policy_name == "dfe-loader-prod-policy"
      error_message = "vault_policy_name must be {canonical_name}-policy"
    }

    assert {
      condition     = output.vault_secret_path == "secret/data/dfe/prod/loader"
      error_message = "vault_secret_path must be secret/data/{project}/{env}/{component}"
    }
  }

  run "common_tags_mandatory_keys" {
    command = plan

    assert {
      condition     = output.common_tags["project"] == "dfe"
      error_message = "common_tags.project must equal var.project"
    }

    assert {
      condition     = output.common_tags["component"] == "loader"
      error_message = "common_tags.component must equal var.component"
    }

    assert {
      condition     = output.common_tags["env"] == "prod"
      error_message = "common_tags.env must equal var.env"
    }

    assert {
      condition     = output.common_tags["managed_by"] == "terraform"
      error_message = "common_tags.managed_by must be 'terraform'"
    }

    assert {
      condition     = output.common_tags["repo"] == "github.com/hyperi-io/dfe-infra"
      error_message = "common_tags.repo must be canonical dfe-infra repo path"
    }
  }

  run "canonical_name_within_30_chars" {
    command = plan

    assert {
      condition     = length(output.canonical_name) <= 30
      error_message = "canonical_name must be <=30 chars (GCP Service Account ID constraint)"
    }
  }

  # --- Failure case: component name would push canonical >30 chars ---
  run "reject_canonical_name_over_30_chars" {
    command = plan

    variables {
      project   = "dfe"
      component = "very-long-component-nm"  # dfe-very-long-component-nm-prod = 31 chars
      env       = "prod"
      cloud     = "aws"
      region    = "local"
    }

    expect_failures = [
      null_resource.validate_canonical_name_length
    ]
  }

  # --- Local/Rancher variant ---
  run "local_cloud_names" {
    command = plan

    variables {
      project   = "dfe"
      component = "receiver"
      env       = "local"
      cloud     = "local"
      region    = "local"
    }

    assert {
      condition     = output.canonical_name == "dfe-receiver-local"
      error_message = "local env canonical name should be dfe-receiver-local"
    }

    assert {
      condition     = output.rancher_cluster_name == "dfe-local-local"
      error_message = "rancher_cluster_name must be {project}-{cloud}-{env}"
    }
  }
  ```

- [ ] **Step 2: Run test to verify it fails (no .tf files yet)**

  ```bash
  cd terraform/modules/tf-naming
  tofu test
  ```

  Expected: An error (e.g. `Error: No configuration files`, `Error: Reference to undeclared output`, or provider init failure) — any error confirms the tests cannot pass yet. The exact message varies by OpenTofu version.

- [ ] **Step 3: Commit the test file**

  ```bash
  git add terraform/modules/tf-naming/tests/naming.tftest.hcl
  git commit -m "test: tf-naming - failing tests for canonical naming standard (Section 9)"
  git push
  ```

---

### Task 3: tf-naming — Implement the Module

**Files:**
- Create: `terraform/modules/tf-naming/variables.tf`
- Create: `terraform/modules/tf-naming/main.tf`
- Create: `terraform/modules/tf-naming/outputs.tf`

- [ ] **Step 1: Write `variables.tf`**

  ```hcl
  # terraform/modules/tf-naming/variables.tf

  terraform {
    required_providers {
      null = {
        source  = "hashicorp/null"
        version = "~> 3.0"
      }
    }
  }

  variable "project" {
    description = "Project identifier. Must be 2-10 lowercase alphanumeric chars starting with a letter. Default: 'dfe'."
    type        = string
    default     = "dfe"

    validation {
      condition     = can(regex("^[a-z][a-z0-9]{1,9}$", var.project))
      error_message = "project must be 2-10 lowercase alphanumeric chars starting with a letter (e.g. 'dfe')."
    }
  }

  variable "component" {
    description = "Component name. 2-15 chars, lowercase letters/digits/hyphens, start and end with letter or digit. Examples: 'loader', 'receiver', 'keda-scaler'."
    type        = string

    validation {
      condition     = can(regex("^[a-z][a-z0-9-]{0,13}[a-z0-9]$", var.component)) || can(regex("^[a-z]{2}$", var.component))
      error_message = "component must be 2-15 chars: lowercase letters, digits, hyphens — start and end with letter or digit."
    }
  }

  variable "env" {
    description = "Deployment environment. Must be one of: dev, stg, prod, local."
    type        = string

    validation {
      condition     = contains(["dev", "stg", "prod", "local"], var.env)
      error_message = "env must be one of: dev, stg, prod, local."
    }
  }

  variable "cloud" {
    description = "Target cloud platform. Must be one of: aws, gcp, az, local."
    type        = string

    validation {
      condition     = contains(["aws", "gcp", "az", "local"], var.cloud)
      error_message = "cloud must be one of: aws, gcp, az, local."
    }
  }

  variable "region" {
    description = "Cloud region identifier (e.g. 'us-east-1', 'europe-west1'). Use 'local' for on-prem Rancher deployments."
    type        = string
    default     = "local"
  }
  ```

- [ ] **Step 2: Write `main.tf`**

  ```hcl
  # terraform/modules/tf-naming/main.tf
  #
  # Derives all platform-specific resource names from four canonical dimensions.
  # Canonical name: {project}-{component}-{env} — max 30 chars (GCP Service Account ID constraint).
  # See spec Section 9 for the full standard.

  locals {
    # Primary canonical identifier used across all platforms
    canonical_name = "${var.project}-${var.component}-${var.env}"

    # Kubernetes
    k8s_namespace       = "${var.project}-${var.env}"
    k8s_service_account = "${var.project}-${var.component}"

    # Rancher (on-prem K8s management)
    rancher_cluster_name = "${var.project}-${var.cloud}-${var.env}"
    rancher_project_name = "${var.project}-${var.env}"

    # AWS: IRSA role name (max 64 chars — canonical_name is ≤30, so -irsa suffix is safe)
    aws_irsa_role_name = "${local.canonical_name}-irsa"

    # GCP: Service Account account_id (max 30 chars — canonical_name is validated ≤30)
    gcp_sa_account_id = local.canonical_name

    # Azure: User-Assigned Managed Identity and Federated Identity Credential
    azure_identity_name       = "id-${local.canonical_name}"
    azure_federated_cred_name = "fc-${local.canonical_name}"

    # OpenBao / Vault (used for local Rancher target; AppRole per service)
    vault_approle_name = local.canonical_name
    vault_policy_name  = "${local.canonical_name}-policy"
    vault_secret_path  = "secret/data/${var.project}/${var.env}/${var.component}"

    # Cloud resource tags (lowercase underscore keys per spec Section 9.3)
    common_tags = {
      project    = var.project
      component  = var.component
      env        = var.env
      cloud      = var.cloud
      region     = var.region
      managed_by = "terraform"
      repo       = "github.com/hyperi-io/dfe-infra"
    }
  }

  # Hard stop if canonical name would exceed 30 chars (GCP Service Account ID limit).
  # This is the binding constraint: AWS allows 64, Azure 128, K8s 63.
  resource "null_resource" "validate_canonical_name_length" {
    lifecycle {
      precondition {
        condition     = length(local.canonical_name) <= 30
        error_message = "Canonical name '${local.canonical_name}' is ${length(local.canonical_name)} chars, exceeds the 30-char GCP Service Account ID limit. Shorten project, component, or env."
      }
    }
  }
  ```

- [ ] **Step 3: Write `outputs.tf`**

  ```hcl
  # terraform/modules/tf-naming/outputs.tf

  output "canonical_name" {
    description = "Primary resource name: {project}-{component}-{env}. Max 30 chars (GCP SA constraint). Used as the base for all platform-specific names."
    value       = local.canonical_name
  }

  output "k8s_namespace" {
    description = "Kubernetes Namespace name: {project}-{env}. e.g. 'dfe-prod'."
    value       = local.k8s_namespace
  }

  output "k8s_service_account" {
    description = "Kubernetes ServiceAccount name: {project}-{component}. e.g. 'dfe-loader'."
    value       = local.k8s_service_account
  }

  output "rancher_cluster_name" {
    description = "Rancher cluster name: {project}-{cloud}-{env}. e.g. 'dfe-local-prod'."
    value       = local.rancher_cluster_name
  }

  output "rancher_project_name" {
    description = "Rancher Project name (maps to K8s namespace group): {project}-{env}."
    value       = local.rancher_project_name
  }

  output "aws_irsa_role_name" {
    description = "AWS IAM Role name for IRSA: {canonical_name}-irsa. e.g. 'dfe-loader-prod-irsa'."
    value       = local.aws_irsa_role_name
  }

  output "gcp_sa_account_id" {
    description = "GCP Service Account account_id (≤30 chars): equals canonical_name. e.g. 'dfe-loader-prod'."
    value       = local.gcp_sa_account_id
  }

  output "azure_identity_name" {
    description = "Azure User-Assigned Managed Identity name: id-{canonical_name}. e.g. 'id-dfe-loader-prod'."
    value       = local.azure_identity_name
  }

  output "azure_federated_cred_name" {
    description = "Azure Federated Identity Credential name: fc-{canonical_name}. e.g. 'fc-dfe-loader-prod'."
    value       = local.azure_federated_cred_name
  }

  output "vault_approle_name" {
    description = "OpenBao/Vault AppRole name: equals canonical_name. e.g. 'dfe-loader-prod'."
    value       = local.vault_approle_name
  }

  output "vault_policy_name" {
    description = "OpenBao/Vault policy name: {canonical_name}-policy. e.g. 'dfe-loader-prod-policy'."
    value       = local.vault_policy_name
  }

  output "vault_secret_path" {
    description = "OpenBao/Vault KV path: secret/data/{project}/{env}/{component}. e.g. 'secret/data/dfe/prod/loader'."
    value       = local.vault_secret_path
  }

  output "common_tags" {
    description = "Map of cloud resource tags (lowercase underscore keys) to apply to all Terraform-managed resources. Mandatory keys: project, component, env, cloud, region, managed_by, repo."
    value       = local.common_tags
  }
  ```

- [ ] **Step 4: Run `tofu init` to install null provider**

  ```bash
  cd terraform/modules/tf-naming
  tofu init
  ```

  Expected: `OpenTofu has been successfully initialized!`

- [ ] **Step 5: Run `tofu test` — verify all tests pass**

  ```bash
  cd terraform/modules/tf-naming
  tofu test
  ```

  Expected output (all 10 runs):
  ```
  Success! 10 passed, 0 failed.
  ```

  If any assert fails, read the error_message, fix the corresponding local in `main.tf`, and re-run.

- [ ] **Step 6: Commit the implementation**

  ```bash
  git add terraform/modules/tf-naming/
  git commit -m "feat: implement tf-naming module (canonical naming standard, Section 9)"
  git push
  ```

---

## Chunk 2: Helm Library + ArgoCD Skeleton + Bootstrap

### Task 4: Helm Library Chart `dfe-common`

**Files:**
- Create: `helm/library/dfe-common/Chart.yaml`
- Create: `helm/library/dfe-common/templates/_labels.tpl`
- Create: `helm/library/dfe-common/templates/_names.tpl`
- Create: `helm/library/dfe-common/tests/lint-test/Chart.yaml`
- Create: `helm/library/dfe-common/tests/lint-test/templates/configmap.yaml`

- [ ] **Step 1: Create `helm/library/dfe-common/Chart.yaml`**

  ```yaml
  # helm/library/dfe-common/Chart.yaml
  apiVersion: v2
  name: dfe-common
  description: Shared Helm template helpers for all DFE charts. Provides commonLabels and fullname macros per spec Section 9.4.
  type: library
  version: 0.1.0
  ```

- [ ] **Step 2: Create `helm/library/dfe-common/templates/_labels.tpl`**

  ```yaml
  {{/*
  dfe-common.labels — standard Kubernetes labels per DFE spec Section 9.4.
  Requires .Values.project, .Values.component, .Values.env, .Values.cloud.
  Usage:
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  */}}
  {{- define "dfe-common.labels" -}}
  app.kubernetes.io/name: {{ printf "%s-%s" .Values.project .Values.component | quote }}
  app.kubernetes.io/part-of: {{ .Values.project | quote }}
  app.kubernetes.io/managed-by: "helm"
  app.kubernetes.io/version: {{ .Chart.AppVersion | default "0.0.0" | quote }}
  dfe.hyperi.io/env: {{ .Values.env | quote }}
  dfe.hyperi.io/cloud: {{ .Values.cloud | quote }}
  {{- end }}

  {{/*
  dfe-common.selectorLabels — minimal stable labels for Deployment selectors.
  Only app.kubernetes.io/name — must not change after first deploy (immutable selector).
  */}}
  {{- define "dfe-common.selectorLabels" -}}
  app.kubernetes.io/name: {{ printf "%s-%s" .Values.project .Values.component | quote }}
  {{- end }}
  ```

- [ ] **Step 3: Create `helm/library/dfe-common/templates/_names.tpl`**

  ```yaml
  {{/*
  dfe-common.fullname — canonical resource name: {project}-{component}.
  Does NOT include env (env goes in namespace, not resource name) to keep names stable.
  */}}
  {{- define "dfe-common.fullname" -}}
  {{- printf "%s-%s" .Values.project .Values.component | trunc 63 | trimSuffix "-" }}
  {{- end }}

  {{/*
  dfe-common.namespace — namespace: {project}-{env}.
  Used when a chart needs to reference its own namespace explicitly.
  */}}
  {{- define "dfe-common.namespace" -}}
  {{- printf "%s-%s" .Values.project .Values.env }}
  {{- end }}

  {{/*
  dfe-common.serviceAccountName — K8s ServiceAccount name: {project}-{component}.
  Same as fullname, explicit for clarity.
  */}}
  {{- define "dfe-common.serviceAccountName" -}}
  {{- printf "%s-%s" .Values.project .Values.component }}
  {{- end }}
  ```

- [ ] **Step 4: Create the lint test chart `Chart.yaml`**

  ```yaml
  # helm/library/dfe-common/tests/lint-test/Chart.yaml
  apiVersion: v2
  name: dfe-common-lint-test
  description: Minimal chart that uses dfe-common library — used to validate helm lint passes
  version: 0.1.0
  appVersion: "0.0.1"
  dependencies:
    - name: dfe-common
      version: "0.1.0"
      repository: "file://../../../"
  ```

- [ ] **Step 5: Create the lint test template**

  ```yaml
  # helm/library/dfe-common/tests/lint-test/templates/configmap.yaml
  apiVersion: v1
  kind: ConfigMap
  metadata:
    name: {{ include "dfe-common.fullname" . }}-test
    namespace: {{ include "dfe-common.namespace" . }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  data:
    fullname: {{ include "dfe-common.fullname" . }}
    namespace: {{ include "dfe-common.namespace" . }}
  ```

- [ ] **Step 6: Create a minimal `values.yaml` for the lint test**

  ```yaml
  # helm/library/dfe-common/tests/lint-test/values.yaml
  project: dfe
  component: test
  env: dev
  cloud: local
  ```

- [ ] **Step 7: Run `helm dependency update` + `helm lint`**

  ```bash
  cd helm/library/dfe-common/tests/lint-test
  helm dependency update
  helm lint .
  ```

  Expected:
  ```
  ==> Linting .
  [INFO] Chart.yaml: icon is recommended
  1 chart(s) linted, 0 chart(s) failed
  ```

- [ ] **Step 8: Verify template renders correctly**

  ```bash
  helm template test . --values values.yaml
  ```

  Expected: ConfigMap YAML with labels:
  ```yaml
  app.kubernetes.io/name: "dfe-test"
  app.kubernetes.io/part-of: "dfe"
  dfe.hyperi.io/env: "dev"
  dfe.hyperi.io/cloud: "local"
  ```

- [ ] **Step 9: Commit**

  ```bash
  git add helm/library/dfe-common/
  git commit -m "feat: add dfe-common Helm library chart (shared labels per Section 9.4)"
  git push
  ```

---

### Task 5: ArgoCD AppProject + Root ApplicationSet

**Files:**
- Create: `argocd/bootstrap/appproject-bootstrap.yaml`
- Create: `argocd/bootstrap/argocd-cluster-addons.yaml`

The root ApplicationSet reads the cluster secret (written by bootstrap.sh) and spawns the layer-specific ApplicationSets from `argocd/appsets/`. The matrix generator pattern is preserved from dfe-core.

- [ ] **Step 1: Create `argocd/bootstrap/appproject-bootstrap.yaml`**

  ```yaml
  # argocd/bootstrap/appproject-bootstrap.yaml
  # Three AppProjects — applied once by bootstrap.sh before ArgoCD manages itself.
  # Grants ArgoCD permission to deploy into the correct namespaces per project.
  ---
  apiVersion: argoproj.io/v1alpha1
  kind: AppProject
  metadata:
    name: infra
    namespace: argocd
    annotations:
      argocd.argoproj.io/sync-wave: "1"
  spec:
    description: "Layer 1 infrastructure: operators, cert-manager, ESO, Envoy Gateway, KEDA"
    sourceRepos:
      - "*"
    destinations:
      - namespace: "*"
        server: https://kubernetes.default.svc
    clusterResourceWhitelist:
      - group: "*"
        kind: "*"
  ---
  apiVersion: argoproj.io/v1alpha1
  kind: AppProject
  metadata:
    name: data
    namespace: argocd
    annotations:
      argocd.argoproj.io/sync-wave: "1"
  spec:
    description: "Layer 2 data platform: Kafka, ClickHouse, CNPG, FerretDB, HyperDX, OTel"
    sourceRepos:
      - "*"
    destinations:
      - namespace: "*"
        server: https://kubernetes.default.svc
    clusterResourceWhitelist:
      - group: "*"
        kind: "*"
  ---
  apiVersion: argoproj.io/v1alpha1
  kind: AppProject
  metadata:
    name: dfe-apps
    namespace: argocd
    annotations:
      argocd.argoproj.io/sync-wave: "1"
  spec:
    description: "DFE application services: dfe-engine, dfe-ui, dfe-receiver, dfe-loader, dfe-archiver, dfe-fetcher"
    sourceRepos:
      - "*"
    destinations:
      - namespace: "*"
        server: https://kubernetes.default.svc
    clusterResourceWhitelist:
      - group: "*"
        kind: "*"
  ```

- [ ] **Step 2: Create `argocd/bootstrap/argocd-cluster-addons.yaml`**

  This is the root ApplicationSet. It reads the cluster secret annotations (written by bootstrap.sh from Terraform outputs) and creates child Applications for each layer's ApplicationSet.

  ```yaml
  # argocd/bootstrap/argocd-cluster-addons.yaml
  # Root ApplicationSet — spawns layer ApplicationSets using cluster secret annotation bridge.
  # The cluster secret (name: dfe-cluster, namespace: argocd) is written by bootstrap.sh
  # with dfe.hyperi.io/* annotations carrying all Terraform outputs.
  apiVersion: argoproj.io/v1alpha1
  kind: ApplicationSet
  metadata:
    name: dfe-cluster-addons
    namespace: argocd
  spec:
    generators:
      - clusters:
          selector:
            matchLabels:
              argocd.argoproj.io/secret-type: cluster
            matchExpressions:
              - key: dfe.hyperi.io/managed
                operator: Exists
    template:
      metadata:
        name: "dfe-addons-{{name}}"
        namespace: argocd
      spec:
        project: infra
        source:
          repoURL: "{{metadata.annotations.dfe\.hyperi\.io/repo_url}}"
          targetRevision: "{{metadata.annotations.dfe\.hyperi\.io/target_revision}}"
          path: argocd/appsets
        destination:
          server: "{{server}}"
          namespace: argocd
        syncPolicy:
          automated:
            prune: true
            selfHeal: true
          syncOptions:
            - CreateNamespace=true
            - ServerSideApply=true
  ```

- [ ] **Step 3: Validate the YAML syntax**

  ```bash
  kubectl apply --dry-run=client -f argocd/bootstrap/appproject-bootstrap.yaml
  kubectl apply --dry-run=client -f argocd/bootstrap/argocd-cluster-addons.yaml
  ```

  Expected: `... configured (dry run)` for each resource. If `kubectl` is not pointed at a cluster, validate YAML syntax only:

  ```bash
  for f in argocd/bootstrap/appproject-bootstrap.yaml argocd/bootstrap/argocd-cluster-addons.yaml; do
    python3 -c "import yaml,sys; list(yaml.safe_load_all(open('$f')))" && echo "OK: $f"
  done
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add argocd/bootstrap/
  git commit -m "feat: add ArgoCD AppProjects (infra/data/dfe-apps) and root cluster-addons ApplicationSet"
  git push
  ```

---

### Task 6: Layer ApplicationSet Skeletons

**Files:**
- Create: `argocd/appsets/layer1-addons.yaml`
- Create: `argocd/appsets/layer2-data.yaml`
- Create: `argocd/appsets/layer2-apps.yaml`
- Create: `argocd/values/common.yaml`
- Create: `argocd/values/local.yaml`

These ApplicationSets use the ArgoCD matrix generator: cross product of `clusters` (the registered cluster, carrying annotations) × a `list` of component definitions. Each template reads `{{metadata.annotations.dfe.hyperi.io/*}}` for cloud-specific values.

- [ ] **Step 1: Create `argocd/appsets/layer1-addons.yaml`**

  ```yaml
  # argocd/appsets/layer1-addons.yaml
  # Wave 2-3: cert-manager (adopted), ESO (adopted), Envoy Gateway, KEDA,
  # metrics-server, VPA, Reloader, CNPG operator, Strimzi operator, ClickHouse operator.
  # "Adopted" components are already installed by bootstrap.sh; this ApplicationSet
  # takes ownership for ongoing sync/upgrade. Boot order is enforced by sync-wave annotations.
  apiVersion: argoproj.io/v1alpha1
  kind: ApplicationSet
  metadata:
    name: dfe-layer1-addons
    namespace: argocd
  spec:
    generators:
      - matrix:
          generators:
            - clusters:
                selector:
                  matchLabels:
                    argocd.argoproj.io/secret-type: cluster
                    dfe.hyperi.io/managed: "true"
            - list:
                elements:
                  # Wave 2: infrastructure controllers (already bootstrapped, ArgoCD adopts)
                  - chart: cert-manager
                    repo: https://charts.jetstack.io
                    version: "v1.14.0"
                    namespace: cert-manager
                    wave: "2"
                    project: infra
                  - chart: external-secrets
                    repo: https://charts.external-secrets.io
                    version: "0.9.13"
                    namespace: external-secrets
                    wave: "2"
                    project: infra
                  - chart: envoy-gateway
                    repo: oci://docker.io/envoyproxy/gateway-helm
                    version: "v1.2.0"
                    namespace: envoy-gateway-system
                    wave: "2"
                    project: infra
                  - chart: external-dns
                    repo: https://kubernetes-sigs.github.io/external-dns/
                    version: "1.14.3"
                    namespace: external-dns
                    wave: "2"
                    project: infra
                  # Wave 3: operators (must follow wave 2 CRD installation)
                  - chart: keda
                    repo: https://kedacore.github.io/charts
                    version: "2.14.0"
                    namespace: keda
                    wave: "3"
                    project: infra
                  - chart: metrics-server
                    repo: https://kubernetes-sigs.github.io/metrics-server/
                    version: "3.12.0"
                    namespace: kube-system
                    wave: "3"
                    project: infra
                  - chart: stakater-reloader
                    repo: https://stakater.github.io/stakater-reloader
                    version: "1.1.0"
                    namespace: reloader
                    wave: "3"
                    project: infra
                  - chart: cnpg
                    repo: https://cloudnative-pg.github.io/charts
                    version: "0.21.0"
                    namespace: cnpg-system
                    wave: "3"
                    project: infra
                  - chart: strimzi-kafka-operator
                    repo: https://strimzi.io/charts/
                    version: "0.40.0"
                    namespace: strimzi
                    wave: "3"
                    project: infra
                  - chart: clickhouse-operator
                    repo: https://docs.altinity.com/clickhouse-operator/
                    version: "0.23.0"
                    namespace: clickhouse-operator
                    wave: "3"
                    project: infra
    template:
      metadata:
        name: "{{chart}}-{{name}}"
        namespace: argocd
        annotations:
          argocd.argoproj.io/sync-wave: "{{wave}}"
      spec:
        project: "{{project}}"
        source:
          repoURL: "{{repo}}"
          chart: "{{chart}}"
          targetRevision: "{{version}}"
          helm:
            valuesObject:
              # Common values injected from cluster secret annotations
              global:
                domain: "{{metadata.annotations.dfe\.hyperi\.io/domain}}"
                cloud: "{{metadata.annotations.dfe\.hyperi\.io/cloud}}"
                env: "{{metadata.annotations.dfe\.hyperi\.io/env}}"
              # Chart-specific value files are added in Plan 02 (layer1-rancher)
              # when each component's actual Helm values are defined.
        destination:
          server: "{{server}}"
          namespace: "{{namespace}}"
        syncPolicy:
          automated:
            prune: false       # operators: never auto-prune CRDs
            selfHeal: true
          syncOptions:
            - CreateNamespace=true
            - ServerSideApply=true
            - RespectIgnoreDifferences=true
  ```

- [ ] **Step 2: Create `argocd/appsets/layer2-data.yaml`**

  ```yaml
  # argocd/appsets/layer2-data.yaml
  # Wave 4: CNPG PostgreSQL cluster, ClickHouse cluster, Strimzi Kafka, FerretDB, OTel Collector.
  # These depend on operators installed in Wave 3.
  apiVersion: argoproj.io/v1alpha1
  kind: ApplicationSet
  metadata:
    name: dfe-layer2-data
    namespace: argocd
  spec:
    generators:
      - matrix:
          generators:
            - clusters:
                selector:
                  matchLabels:
                    argocd.argoproj.io/secret-type: cluster
                    dfe.hyperi.io/managed: "true"
            - list:
                elements:
                  - app: cnpg-cluster
                    namespace: "dfe-{{metadata.annotations.dfe\.hyperi\.io/env}}"
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
                    namespace: "dfe-{{metadata.annotations.dfe\.hyperi\.io/env}}"
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
    template:
      metadata:
        name: "{{app}}-{{name}}"
        namespace: argocd
        annotations:
          argocd.argoproj.io/sync-wave: "{{wave}}"
      spec:
        project: "{{project}}"
        source:
          repoURL: "{{metadata.annotations.dfe\.hyperi\.io/repo_url}}"
          targetRevision: "{{metadata.annotations.dfe\.hyperi\.io/target_revision}}"
          path: "helm/charts/{{app}}"
          helm:
            valueFiles:
              - "../../argocd/values/common.yaml"
              - "../../argocd/values/{{metadata.annotations.dfe\.hyperi\.io/cloud}}.yaml"
        destination:
          server: "{{server}}"
          namespace: "{{namespace}}"
        syncPolicy:
          automated:
            prune: true
            selfHeal: true
          syncOptions:
            - CreateNamespace=true
            - ServerSideApply=true
  ```

- [ ] **Step 3: Create `argocd/appsets/layer2-apps.yaml`**

  ```yaml
  # argocd/appsets/layer2-apps.yaml
  # Wave 5: DFE application services — dfe-engine, dfe-ui, dfe-receiver,
  # dfe-loader, dfe-archiver, dfe-fetcher. dfe-transform-* added on demand.
  # Helm values are compiled by dfe-engine's HelmValuesCompiler and written to
  # the config storage backend (NFS/S3/GCS/Azure Files).
  apiVersion: argoproj.io/v1alpha1
  kind: ApplicationSet
  metadata:
    name: dfe-layer2-apps
    namespace: argocd
  spec:
    generators:
      - matrix:
          generators:
            - clusters:
                selector:
                  matchLabels:
                    argocd.argoproj.io/secret-type: cluster
                    dfe.hyperi.io/managed: "true"
            - list:
                elements:
                  - app: dfe-engine
                    wave: "5"
                  - app: dfe-ui
                    wave: "5"
                  - app: dfe-receiver
                    wave: "5"
                  - app: dfe-loader
                    wave: "5"
                  - app: dfe-archiver
                    wave: "5"
                  - app: dfe-fetcher
                    wave: "5"
    template:
      metadata:
        name: "{{app}}-{{name}}"
        namespace: argocd
        annotations:
          argocd.argoproj.io/sync-wave: "{{wave}}"
      spec:
        project: dfe-apps
        source:
          repoURL: "{{metadata.annotations.dfe\.hyperi\.io/repo_url}}"
          targetRevision: "{{metadata.annotations.dfe\.hyperi\.io/target_revision}}"
          path: "helm/charts/{{app}}"
          helm:
            valueFiles:
              - "../../argocd/values/common.yaml"
              - "../../argocd/values/{{metadata.annotations.dfe\.hyperi\.io/cloud}}.yaml"
        destination:
          server: "{{server}}"
          namespace: "{{metadata.annotations.dfe\.hyperi\.io/dfe_namespace}}"
        syncPolicy:
          automated:
            prune: true
            selfHeal: true
          syncOptions:
            - CreateNamespace=true
            - ServerSideApply=true
  ```

- [ ] **Step 4: Create `argocd/values/common.yaml`**

  ```yaml
  # argocd/values/common.yaml
  # Default Helm values shared across all cloud targets.
  # Cloud-specific overrides in local.yaml / aws.yaml / gcp.yaml / azure.yaml.
  # These values are read by all Layer 2 charts via ApplicationSet valueFiles.

  global:
    project: dfe
    # env, cloud, domain, region are injected from cluster secret annotations at deploy time
    env: ""
    cloud: ""
    domain: ""
    region: local

  # OTel Collector endpoint — used by all DFE services for OTLP gRPC export
  otel:
    endpoint: "otel-collector-gateway.otel.svc.cluster.local:4317"
    tls: false

  # Kafka bootstrap — Strimzi cluster in strimzi namespace (KRaft, SASL/SCRAM)
  kafka:
    bootstrapServers: "dfe-kafka-kafka-bootstrap.strimzi.svc.cluster.local:9092"
    sasl:
      enabled: true
      mechanism: SCRAM-SHA-512

  # ClickHouse endpoint
  clickhouse:
    host: "clickhouse.clickhouse.svc.cluster.local"
    port: 8123
    database: dfe

  # CNPG PostgreSQL — shared cluster for FerretDB + HyperDX metadata + dfe-engine
  postgresql:
    host: "cnpg-cluster-rw.dfe-prod.svc.cluster.local"
    port: 5432
    database: dfe

  # Receiver network: CGNAT range 100.64.0.0/10 per spec Section 2.9
  receiver:
    listenCIDR: "100.64.0.0/10"

  # KEDA scaling bounds (dev profile defaults; overridden by tenancy sizing)
  keda:
    minReplicas: 1
    maxReplicas: 5
    scalingPressureThreshold: "0.7"
  ```

- [ ] **Step 5: Create `argocd/values/local.yaml`**

  ```yaml
  # argocd/values/local.yaml
  # Rancher local (RKE2) target overrides.
  global:
    cloud: local
    region: local

  # local-path-provisioner is the default StorageClass on RKE2
  storageClass: local-path

  # Envoy Gateway uses externalIPs (not cloud LB) on Rancher local
  envoyGateway:
    service:
      type: LoadBalancer
      externalIPs: []  # set by bootstrap.sh from cluster node IP

  # OpenBao (local secrets backend) endpoint
  vault:
    address: "https://bao.devex.hyperi.io:8200"
    # token injected via ESO ClusterSecretStore (AppRole auth)

  # NFS config storage (from storage VM, NFS export /data/dfe-config)
  configStorage:
    type: nfs
    server: "storage.devex.hyperi.io"
    path: /data/dfe-config
  ```

- [ ] **Step 6: Validate YAML syntax on all appsets**

  ```bash
  for f in argocd/appsets/*.yaml argocd/values/*.yaml argocd/bootstrap/*.yaml; do
    python3 -c "import yaml,sys; list(yaml.safe_load_all(open('$f')))" && echo "OK: $f"
  done
  ```

  Expected: `OK: <each file>` with no exceptions.

- [ ] **Step 7: Commit**

  ```bash
  git add argocd/
  git commit -m "feat: add ArgoCD ApplicationSet skeletons (layer1/layer2-data/layer2-apps) + common values"
  git push
  ```

---

### Task 7: Bootstrap Script + Cluster Secret Template

**Files:**
- Create: `bootstrap/templates/cluster-secret.yaml.tpl`
- Create: `bootstrap/templates/eso-cluster-secret-store.yaml.tpl`
- Create: `bootstrap/bootstrap.sh`

The cluster secret is the annotation bridge from Terraform to ArgoCD. `bootstrap.sh` renders the template using `envsubst`, writes the secret, then installs ArgoCD and applies the root ApplicationSet.

- [ ] **Step 1: Create `bootstrap/templates/cluster-secret.yaml.tpl`**

  ```yaml
  # bootstrap/templates/cluster-secret.yaml.tpl
  # Rendered by bootstrap.sh via envsubst. All required vars must be set before running.
  # This secret registers the cluster with ArgoCD AND carries all Terraform outputs
  # as dfe.hyperi.io/* annotations, which ApplicationSets read via {{ .metadata.annotations.* }}.
  apiVersion: v1
  kind: Secret
  metadata:
    name: dfe-cluster
    namespace: argocd
    labels:
      argocd.argoproj.io/secret-type: cluster
      dfe.hyperi.io/managed: "true"
    annotations:
      # Identity
      dfe.hyperi.io/env: "${DFE_ENV}"
      dfe.hyperi.io/cloud: "${DFE_CLOUD}"
      dfe.hyperi.io/region: "${DFE_REGION}"
      dfe.hyperi.io/domain: "${DFE_DOMAIN}"
      dfe.hyperi.io/tenancy: "${DFE_TENANCY}"
      # GitOps source
      dfe.hyperi.io/repo_url: "${DFE_REPO_URL}"
      dfe.hyperi.io/target_revision: "${DFE_TARGET_REVISION}"
      # Infrastructure outputs (from Terraform)
      dfe.hyperi.io/storage_class: "${DFE_STORAGE_CLASS}"
      dfe.hyperi.io/dfe_namespace: "${DFE_NAMESPACE}"
      dfe.hyperi.io/clickhouse_host: "${DFE_CLICKHOUSE_HOST}"
      dfe.hyperi.io/kafka_bootstrap: "${DFE_KAFKA_BOOTSTRAP}"
      dfe.hyperi.io/otel_endpoint: "${DFE_OTEL_ENDPOINT}"
      # Workload identity annotations JSON (from tf-iam output)
      # Format: {"dfe-loader": {"eks.amazonaws.com/role-arn": "arn:..."}, ...}
      dfe.hyperi.io/workload_identity_annotations: "${DFE_WORKLOAD_IDENTITY_ANNOTATIONS}"
  type: Opaque
  stringData:
    # In-cluster server — ArgoCD manages this cluster itself
    name: "dfe-${DFE_CLOUD}-${DFE_ENV}"
    server: https://kubernetes.default.svc
    config: |
      {
        "tlsClientConfig": {"insecure": false}
      }
  ```

- [ ] **Step 2: Create `bootstrap/templates/eso-cluster-secret-store.yaml.tpl`**

  ```yaml
  # bootstrap/templates/eso-cluster-secret-store.yaml.tpl
  # ESO ClusterSecretStore — configures ESO to pull secrets from the target backend.
  # For local/Rancher: OpenBao (Vault-compatible) AppRole auth.
  # For cloud targets: replace provider block with aws/gcp/azure provider (per cloud.yaml values).
  apiVersion: external-secrets.io/v1beta1
  kind: ClusterSecretStore
  metadata:
    name: dfe-secret-store
  spec:
    provider:
      # LOCAL / RANCHER: OpenBao AppRole
      vault:
        server: "${DFE_VAULT_ADDR}"
        path: "secret"
        version: "v2"
        auth:
          appRole:
            path: approle
            roleId: "${DFE_VAULT_ROLE_ID}"
            secretRef:
              name: dfe-vault-approle-secret
              namespace: external-secrets
              key: roleSecretID
  ```

- [ ] **Step 3: Create `bootstrap/bootstrap.sh`**

  ```bash
  #!/usr/bin/env bash
  # bootstrap.sh — Idempotent DFE cluster bootstrap.
  # All steps use 'helm upgrade --install' or 'kubectl apply' to be safe to re-run.
  #
  # Required environment variables (set before running, or export from Terraform outputs):
  #   DFE_ENV                  dev | stg | prod | local
  #   DFE_CLOUD                aws | gcp | az | local
  #   DFE_REGION               e.g. us-east-1, local
  #   DFE_DOMAIN               e.g. devex.hyperi.io
  #   DFE_TENANCY              dev | small | large
  #   DFE_REPO_URL             Git repo URL for ArgoCD (e.g. https://github.com/catinspace-au/dfe-infra)
  #   DFE_TARGET_REVISION      Git branch/tag (e.g. main)
  #   DFE_STORAGE_CLASS        e.g. local-path (Rancher), gp3 (AWS), standard (GCP)
  #   DFE_NAMESPACE            K8s namespace for DFE apps, e.g. dfe-prod
  #   DFE_CLICKHOUSE_HOST      ClickHouse service hostname
  #   DFE_KAFKA_BOOTSTRAP      Kafka bootstrap servers string
  #   DFE_OTEL_ENDPOINT        OTel Collector gRPC endpoint (host:port)
  #   DFE_VAULT_ADDR           OpenBao/Vault address (local: https://bao.devex.hyperi.io:8200)
  #   DFE_VAULT_ROLE_ID        ESO AppRole role_id
  #   DFE_WORKLOAD_IDENTITY_ANNOTATIONS  JSON map of service → cloud identity annotations
  #
  # Optional:
  #   DFE_DRY_RUN=true         Print commands without executing (for CI validation)

  set -euo pipefail

  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  TEMPLATES_DIR="${SCRIPT_DIR}/templates"

  # Dry-run wrapper
  run() {
    if [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
      echo "[DRY-RUN] $*"
    else
      "$@"
    fi
  }

  # Validate required variables
  required_vars=(
    DFE_ENV DFE_CLOUD DFE_REGION DFE_DOMAIN DFE_TENANCY
    DFE_REPO_URL DFE_TARGET_REVISION
    DFE_STORAGE_CLASS DFE_NAMESPACE
    DFE_CLICKHOUSE_HOST DFE_KAFKA_BOOTSTRAP DFE_OTEL_ENDPOINT
    DFE_VAULT_ADDR DFE_VAULT_ROLE_ID
    DFE_WORKLOAD_IDENTITY_ANNOTATIONS
  )
  missing=()
  for var in "${required_vars[@]}"; do
    [[ -z "${!var:-}" ]] && missing+=("$var")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "ERROR: Missing required environment variables: ${missing[*]}" >&2
    exit 1
  fi

  # Add Helm repos (idempotent — || true suppresses "already exists" error)
  echo "==> [0/7] Adding Helm repositories"
  run helm repo add jetstack https://charts.jetstack.io || true
  run helm repo add external-secrets https://charts.external-secrets.io || true
  run helm repo add argo https://argoproj.github.io/argo-helm || true
  run helm repo update

  echo "==> [1/7] Applying ArgoCD namespace + cluster secret"
  run kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
  envsubst < "${TEMPLATES_DIR}/cluster-secret.yaml.tpl" | run kubectl apply -f -

  echo "==> [2/7] Installing cert-manager (idempotent)"
  run helm upgrade --install cert-manager jetstack/cert-manager \
    --namespace cert-manager --create-namespace \
    --version v1.14.0 \
    --set installCRDs=true \
    --wait --timeout 5m

  echo "==> [3/7] Installing external-secrets (idempotent)"
  run helm upgrade --install external-secrets external-secrets/external-secrets \
    --namespace external-secrets --create-namespace \
    --version 0.9.13 \
    --wait --timeout 5m

  echo "==> [4/7] Applying ESO ClusterSecretStore"
  envsubst < "${TEMPLATES_DIR}/eso-cluster-secret-store.yaml.tpl" | run kubectl apply -f -

  # Valkey MUST be installed before ArgoCD — ArgoCD starts with --wait and
  # will timeout if the externalRedis host is unreachable on first boot.
  echo "==> [5/7] Installing Valkey (ArgoCD cache, replaces Redis)"
  run helm upgrade --install dfe-valkey oci://registry-1.docker.io/bitnamicharts/valkey \
    --namespace argocd --create-namespace \
    --version 1.0.0 \
    --set auth.enabled=false \
    --wait --timeout 5m

  echo "==> [6/7] Installing ArgoCD with Valkey cache (idempotent)"
  run helm upgrade --install argocd argo/argo-cd \
    --namespace argocd --create-namespace \
    --version 7.3.0 \
    --set redis.enabled=false \
    --set "externalRedis.host=dfe-valkey-master.argocd.svc.cluster.local" \
    --set "externalRedis.port=6379" \
    --wait --timeout 10m

  echo "==> [7/7] Applying ArgoCD AppProjects + bootstrap ApplicationSet"
  run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/appproject-bootstrap.yaml"
  run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/argocd-cluster-addons.yaml"

  echo "Bootstrap complete — ArgoCD will now sync Layer 1 and Layer 2"
  echo "    ArgoCD UI: https://argocd.${DFE_DOMAIN}"
  echo "    Watch sync: kubectl -n argocd get app -w"
  ```

- [ ] **Step 4: Make bootstrap script executable and validate syntax**

  ```bash
  chmod +x bootstrap/bootstrap.sh
  bash -n bootstrap/bootstrap.sh
  ```

  Expected: No output (bash -n only reports syntax errors)

- [ ] **Step 5: Run dry-run validation of bootstrap script**

  ```bash
  export DFE_ENV=local DFE_CLOUD=local DFE_REGION=local
  export DFE_DOMAIN=devex.hyperi.io DFE_TENANCY=dev
  export DFE_REPO_URL=https://github.com/catinspace-au/dfe-infra
  export DFE_TARGET_REVISION=main
  export DFE_STORAGE_CLASS=local-path DFE_NAMESPACE=dfe-local
  export DFE_CLICKHOUSE_HOST=clickhouse.clickhouse.svc.cluster.local
  export DFE_KAFKA_BOOTSTRAP=dfe-kafka-kafka-bootstrap.strimzi.svc.cluster.local:9092
  export DFE_OTEL_ENDPOINT=otel-collector-gateway.otel.svc.cluster.local:4317
  export DFE_VAULT_ADDR=https://bao.devex.hyperi.io:8200
  export DFE_VAULT_ROLE_ID=test-role-id
  export DFE_WORKLOAD_IDENTITY_ANNOTATIONS='{}'
  export DFE_DRY_RUN=true
  bash bootstrap/bootstrap.sh
  ```

  Expected: Prints `[DRY-RUN] kubectl ...` for each step, no errors.

- [ ] **Step 6: Commit**

  ```bash
  git add bootstrap/
  git commit -m "feat: add idempotent bootstrap.sh and cluster secret / ESO templates"
  git push
  ```

---

## Chunk 3: CI Workflows + License Review

### Task 8: GitHub Actions — Terraform Validate

**Files:**
- Create: `.github/workflows/tf-validate.yml`

- [ ] **Step 1: Create `.github/workflows/tf-validate.yml`**

  ```yaml
  # .github/workflows/tf-validate.yml
  # Runs on every PR and push to main.
  # Validates all Terraform modules: init + validate.
  # Runs tofu test on modules that have .tftest.hcl files.
  name: Terraform Validate

  on:
    push:
      branches: [main]
      paths:
        - 'terraform/**'
        - '.github/workflows/tf-validate.yml'
    pull_request:
      paths:
        - 'terraform/**'

  jobs:
    validate:
      name: tofu validate + test
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4

        - name: Install OpenTofu
          uses: opentofu/setup-opentofu@v1
          with:
            tofu_version: "1.7.0"

        - name: Validate all modules
          run: |
            set -e
            for dir in terraform/modules/*/; do
              echo "==> Validating $dir"
              tofu -chdir="$dir" init -backend=false
              tofu -chdir="$dir" validate
            done

        - name: Run OpenTofu tests (modules with .tftest.hcl)
          run: |
            set -e
            for dir in terraform/modules/*/; do
              if ls "${dir}tests/"*.tftest.hcl 2>/dev/null | grep -q .; then
                echo "==> Testing $dir"
                tofu -chdir="$dir" test
              fi
            done
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add .github/workflows/tf-validate.yml
  git commit -m "ci: add GitHub Actions workflow for OpenTofu validate + test"
  git push
  ```

---

### Task 9: GitHub Actions — Helm Lint

**Files:**
- Create: `.github/workflows/helm-lint.yml`

- [ ] **Step 1: Create `.github/workflows/helm-lint.yml`**

  ```yaml
  # .github/workflows/helm-lint.yml
  # Lints all Helm charts on every PR and push to main.
  # For library charts: uses the lint-test chart in tests/.
  name: Helm Lint

  on:
    push:
      branches: [main]
      paths:
        - 'helm/**'
        - '.github/workflows/helm-lint.yml'
    pull_request:
      paths:
        - 'helm/**'

  jobs:
    lint:
      name: helm lint
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4

        - name: Install Helm
          uses: azure/setup-helm@v4
          with:
            version: "3.14.0"

        - name: Lint library charts via test charts
          run: |
            set -e
            for test_dir in helm/library/*/tests/lint-test/; do
              echo "==> Linting via $test_dir"
              helm dependency update "$test_dir"
              helm lint "$test_dir"
            done

        - name: Lint application charts
          run: |
            set -e
            for chart_dir in helm/charts/*/; do
              if [[ -f "$chart_dir/Chart.yaml" ]]; then
                echo "==> Linting $chart_dir"
                helm dependency update "$chart_dir" 2>/dev/null || true
                helm lint "$chart_dir"
              fi
            done

        - name: Validate YAML syntax of ArgoCD manifests
          run: |
            set -e
            for f in argocd/**/*.yaml argocd/*.yaml; do
              [[ -f "$f" ]] || continue
              python3 -c "import yaml,sys; list(yaml.safe_load_all(open('$f')))" && echo "OK: $f"
            done
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add .github/workflows/helm-lint.yml
  git commit -m "ci: add GitHub Actions workflow for Helm lint + ArgoCD YAML validation"
  git push
  ```

---

### Task 10: License Review Document + Final Push

**Files:**
- Create: `docs/license-review.md`

- [ ] **Step 1: Create `docs/license-review.md`**

  ```markdown
  # License Review

  **Policy:** https://github.com/hyperi-io/licensing
  **Status:** Initial review pending

  ## Review Checklist

  Before each release, audit all dependency licenses against the approved policy.
  Flag GPL/AGPL/SSPL — these require legal sign-off before inclusion.

  ## Terraform Providers

  | Provider | License | Status |
  |----------|---------|--------|
  | hashicorp/null | MPL-2.0 | Approved |
  | (others added per module) | | |

  ## Helm Chart Dependencies (upstream)

  | Chart | License | Status |
  |-------|---------|--------|
  | cert-manager | Apache-2.0 | Approved |
  | external-secrets | Apache-2.0 | Approved |
  | envoy-gateway | Apache-2.0 | Approved |
  | keda | Apache-2.0 | Approved |
  | argo-cd | Apache-2.0 | Approved |
  | strimzi-kafka-operator | Apache-2.0 | Approved |
  | cnpg | Apache-2.0 | Approved |
  | clickhouse-operator | Apache-2.0 | Approved |
  | stakater-reloader | Apache-2.0 | Approved |
  | metrics-server | Apache-2.0 | Approved |
  | hyperdx | MIT | Approved |
  | ferretdb | Apache-2.0 | Approved |
  | valkey (bitnami) | Apache-2.0 | Approved |
  | otel-collector | Apache-2.0 | Approved |
  | kedify-agent | TBD — review before production | Pending |
  | vpa (vertical-pod-autoscaler) | Apache-2.0 | Approved |

  ## Container Images

  To be audited per release. Flag any images with commercial-only licenses.

  ## Notes

  - Kedify OTEL Scaler license must be verified before production use (see Open Question 1 in spec).
  - All Apache-2.0 and MIT licenses are pre-approved per hyperi-io/licensing policy.
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add docs/license-review.md
  git commit -m "docs: add license review tracking document (OSS policy ref hyperi-io/licensing)"
  git push
  ```

---

### Task 11: Cloud Value Stubs (aws / gcp / azure)

**Files:**
- Create: `argocd/values/aws.yaml`
- Create: `argocd/values/gcp.yaml`
- Create: `argocd/values/azure.yaml`

The Layer 2 ApplicationSets resolve `argocd/values/{{cloud}}.yaml` at sync time. Without these files ArgoCD sync fails for any non-local cloud target. These are stubs now — content comes in the cloud-specific plans (Plans 07+).

- [ ] **Step 1: Create `argocd/values/aws.yaml`**

  ```yaml
  # argocd/values/aws.yaml
  # AWS EKS overrides — populated in Plan 07 (aws-eks).
  # Stub required so Layer 2 ApplicationSet valueFiles resolution succeeds on EKS targets.
  global:
    cloud: aws

  storageClass: gp3

  envoyGateway:
    service:
      type: LoadBalancer
      annotations:
        service.beta.kubernetes.io/aws-load-balancer-type: "nlb"

  configStorage:
    type: s3
    # bucket: set from cluster secret annotation dfe.hyperi.io/config_bucket

  # Secrets backend: AWS Secrets Manager via ESO
  # ESO ClusterSecretStore provider block: see bootstrap/templates/eso-cluster-secret-store-aws.yaml.tpl (Plan 07)
  ```

- [ ] **Step 2: Create `argocd/values/gcp.yaml`**

  ```yaml
  # argocd/values/gcp.yaml
  # GCP GKE overrides — populated in Plan 07 (gcp).
  global:
    cloud: gcp

  storageClass: standard-rwo

  envoyGateway:
    service:
      type: LoadBalancer
      # GCP assigns external IP automatically

  configStorage:
    type: gcs
    # bucket: set from cluster secret annotation

  # Secrets backend: GCP Secret Manager via ESO (Plan 07)
  ```

- [ ] **Step 3: Create `argocd/values/azure.yaml`**

  ```yaml
  # argocd/values/azure.yaml
  # Azure AKS overrides — populated in Plan 07 (azure).
  global:
    cloud: az

  storageClass: managed-premium

  envoyGateway:
    service:
      type: LoadBalancer

  configStorage:
    type: azurefiles
    # shareName: set from cluster secret annotation

  # Secrets backend: Azure Key Vault via ESO (Plan 07)
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add argocd/values/aws.yaml argocd/values/gcp.yaml argocd/values/azure.yaml
  git commit -m "chore: add cloud value stubs for aws/gcp/azure (content in cloud-specific plans)"
  git push
  ```

---

### Task 12: Project Directory Structure Document

**Files:**
- Create: `DIRECTORY.md`

- [ ] **Step 1: Create `DIRECTORY.md`**

  ```markdown
  # Repository Directory Structure

  This document is the canonical reference for what lives where in `dfe-infra`.
  Every directory has one owner and one purpose. Do not mix concerns.

  ```
  dfe-infra/
  ├── terraform/
  │   └── modules/              # Reusable Terraform modules (one per cloud resource type)
  │       ├── tf-naming/        # Canonical naming standard — import into every other module
  │       ├── tf-k8s-cluster/   # K8s cluster provisioning (RKE2 / EKS / GKE / AKS)
  │       ├── tf-networking/    # VPC/VNet, subnets, NAT gateway
  │       ├── tf-secrets/       # Secrets backend setup (OpenBao / AWS SM / GCP SM / Azure KV)
  │       ├── tf-storage/       # Cloud storage buckets (config, archiving, backups)
  │       ├── tf-iam/           # Workload identity per DFE service (IRSA/WIF/Azure WI/AppRole)
  │       ├── tf-dns/           # DNS zone + records (Route53 / Cloud DNS / Azure DNS)
  │       └── tf-certs/         # TLS wildcard cert via cert-manager ACME DNS-01
  │
  ├── helm/
  │   ├── library/
  │   │   └── dfe-common/       # Shared Helm helpers (labels, names) — type: library chart
  │   └── charts/               # One chart per DFE service or data component
  │       ├── dfe-engine/
  │       ├── dfe-ui/
  │       ├── dfe-receiver/
  │       ├── dfe-loader/
  │       ├── dfe-archiver/
  │       ├── dfe-fetcher/
  │       ├── cnpg-cluster/     # CNPG PostgreSQL 17 cluster CRD
  │       ├── clickhouse-cluster/  # ClickHouse cluster CRD (Altinity operator)
  │       ├── strimzi-kafka/    # Strimzi Kafka cluster CRD (KRaft, SASL/SCRAM)
  │       ├── ferretdb/         # FerretDB deployment (MongoDB wire protocol over CNPG PG17)
  │       ├── hyperdx/          # HyperDX deployment (observability UI)
  │       └── otel-collector/   # OTel Collector (DaemonSet + Gateway tiers)
  │
  ├── argocd/
  │   ├── bootstrap/            # Applied once by bootstrap.sh before ArgoCD manages itself
  │   │   ├── appproject-bootstrap.yaml   # AppProjects: infra, data, dfe-apps
  │   │   └── argocd-cluster-addons.yaml  # Root ApplicationSet (reads cluster secret)
  │   ├── appsets/              # Layer ApplicationSets (matrix generator pattern)
  │   │   ├── layer1-addons.yaml    # Wave 2-3: operators and infrastructure controllers
  │   │   ├── layer2-data.yaml      # Wave 4: data platform services
  │   │   └── layer2-apps.yaml      # Wave 5: DFE application services
  │   └── values/               # Helm value overrides per cloud target
  │       ├── common.yaml           # Defaults — all clouds inherit these
  │       ├── local.yaml            # Rancher local (RKE2) overrides
  │       ├── aws.yaml              # AWS EKS overrides
  │       ├── gcp.yaml              # GCP GKE overrides
  │       └── azure.yaml            # Azure AKS overrides
  │
  ├── bootstrap/
  │   ├── bootstrap.sh          # Idempotent cluster bootstrap — run once per cluster
  │   └── templates/            # envsubst templates rendered by bootstrap.sh
  │       ├── cluster-secret.yaml.tpl            # ArgoCD cluster secret (annotation bridge)
  │       └── eso-cluster-secret-store.yaml.tpl  # ESO ClusterSecretStore
  │
  ├── docs/
  │   ├── superpowers/
  │   │   ├── specs/            # Design specifications (approved before planning)
  │   │   └── plans/            # Implementation plans (this directory)
  │   ├── 01-dfe-core-analysis.md      # Research corpus — DFE 2.1 analysis
  │   ├── 02-dfe-engine-analysis.md
  │   ├── 03-hyperi-rustlib-analysis.md
  │   ├── 04-dfe-ui-analysis.md
  │   ├── 05-best-practices-research.md
  │   ├── 06-hyperi-infra-analysis.md
  │   ├── 07-synthesis.md
  │   └── license-review.md     # OSS license audit (update per release)
  │
  ├── .github/
  │   └── workflows/
  │       ├── tf-validate.yml   # OpenTofu validate + test on every PR
  │       ├── helm-lint.yml     # Helm lint on every PR
  │       └── docker-build.yml  # Build + push images to JFrog registry on merge to main
  │
  ├── DIRECTORY.md   ← this file
  ├── SCOPE.md       # Project scope and constraints
  └── TL-ARCHITECTURE1.mermaid  # Top-level architecture dependency diagram
  ```

  ## Conventions

  | Convention | Rule |
  |-----------|------|
  | Naming | All resource names derived from tf-naming module. See spec Section 9. |
  | Cloud values | Cloud-specific Helm values go in `argocd/values/{cloud}.yaml` only — never in charts. |
  | Secrets | Never commit secrets. All secrets via ESO. Local dev: use `.env.example` as template. |
  | Chart deps | All charts depend on `helm/library/dfe-common` for labels and names. |
  | Test placement | Terraform tests: `terraform/modules/{module}/tests/`. Helm tests: `helm/charts/{chart}/templates/tests/`. |
  | Registry | Container images pulled from JFrog registry (`global.registry` in `common.yaml`). Future: GHCR when OSS. |
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add DIRECTORY.md
  git commit -m "docs: add DIRECTORY.md (canonical repo structure reference)"
  git push
  ```

---

### Task 13: JFrog Container Registry Configuration

**Files:**
- Modify: `argocd/values/common.yaml` — add `global.registry`
- Create: `bootstrap/templates/regcred.yaml.tpl` — imagePullSecret template
- Create: `.github/workflows/docker-build.yml` — build + push to JFrog

**Note:** Replace `<JFROG_HOST>` below with the actual JFrog hostname (e.g. `hyperi.jfrog.io` or a custom domain). This value also goes in `argocd/values/common.yaml` as `global.registry`.

- [ ] **Step 1: Add `global.registry` to `argocd/values/common.yaml`**

  Add this block immediately after `global:` in `argocd/values/common.yaml`:

  ```yaml
  global:
    project: dfe
    env: ""
    cloud: ""
    domain: ""
    region: local
    # Container registry — JFrog now, GHCR when OSS release is complete.
    # All Helm charts reference: image.registry: "{{ .Values.global.registry }}"
    # Migration to GHCR: change this one value in common.yaml.
    registry: "<JFROG_HOST>/dfe"
  ```

- [ ] **Step 2: Create `bootstrap/templates/regcred.yaml.tpl`**

  bootstrap.sh creates this imagePullSecret before ArgoCD deploys. ESO syncs the JFrog token from the secrets backend.

  ```yaml
  # bootstrap/templates/regcred.yaml.tpl
  # Rendered by bootstrap.sh to create imagePullSecret for JFrog registry.
  # The JFrog token is pulled from the secrets backend by ESO and stored as:
  #   dfe-regcred (namespace: each DFE namespace)
  # This template creates the initial secret before ESO is fully operational.
  apiVersion: v1
  kind: Secret
  metadata:
    name: dfe-regcred
    namespace: ${TARGET_NAMESPACE}
  type: kubernetes.io/dockerconfigjson
  stringData:
    .dockerconfigjson: |
      {
        "auths": {
          "${DFE_REGISTRY_HOST}": {
            "username": "${DFE_REGISTRY_USER}",
            "password": "${DFE_REGISTRY_TOKEN}",
            "auth": "$(echo -n "${DFE_REGISTRY_USER}:${DFE_REGISTRY_TOKEN}" | base64)"
          }
        }
      }
  ```

- [ ] **Step 3: Add registry vars to bootstrap.sh required_vars**

  In `bootstrap/bootstrap.sh`, add to the `required_vars` array:
  ```bash
  DFE_REGISTRY_HOST DFE_REGISTRY_USER DFE_REGISTRY_TOKEN
  ```

  Add a new step after `[4/7]` (ESO ClusterSecretStore) to create the regcred in the ArgoCD and DFE namespaces:

  ```bash
  echo "==> [4b/7] Creating imagePullSecret for JFrog registry"
  for ns in argocd "${DFE_NAMESPACE}" strimzi clickhouse otel hyperdx; do
    run kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
    TARGET_NAMESPACE="$ns" envsubst < "${TEMPLATES_DIR}/regcred.yaml.tpl" | run kubectl apply -f -
  done
  ```

- [ ] **Step 4: Create `.github/workflows/docker-build.yml`**

  ```yaml
  # .github/workflows/docker-build.yml
  # Builds and pushes DFE service images to JFrog on merge to main.
  # This workflow is a stub — individual services (dfe-engine, dfe-ui, etc.) add
  # their build context in their own repos. This workflow covers any Dockerfiles
  # that live directly in dfe-infra (e.g. utility images like ImperativeOperations).
  #
  # Future: when OSS release is complete, change registry to ghcr.io/hyperi-io
  # by updating global.registry in argocd/values/common.yaml and JFROG_HOST secret.
  name: Docker Build + Push

  on:
    push:
      branches: [main]
      paths:
        - 'docker/**'
        - '.github/workflows/docker-build.yml'

  env:
    REGISTRY: ${{ secrets.JFROG_HOST }}
    IMAGE_PREFIX: ${{ secrets.JFROG_HOST }}/dfe

  jobs:
    build:
      name: build and push
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4

        - name: Log in to JFrog registry
          uses: docker/login-action@v3
          with:
            registry: ${{ env.REGISTRY }}
            username: ${{ secrets.JFROG_USER }}
            password: ${{ secrets.JFROG_TOKEN }}

        - name: Set up Docker Buildx
          uses: docker/setup-buildx-action@v3

        - name: Build and push images
          run: |
            set -e
            for dockerfile in docker/*/Dockerfile; do
              service=$(basename "$(dirname "$dockerfile")")
              echo "==> Building $service"
              docker buildx build \
                --platform linux/amd64,linux/arm64 \
                --tag "${IMAGE_PREFIX}/${service}:${GITHUB_SHA::8}" \
                --tag "${IMAGE_PREFIX}/${service}:latest" \
                --push \
                "$dockerfile"
            done
  ```

  **Required GitHub secrets to configure** (Settings → Secrets → Actions):
  - `JFROG_HOST` — JFrog registry hostname (e.g. `hyperi.jfrog.io`)
  - `JFROG_USER` — JFrog service account username
  - `JFROG_TOKEN` — JFrog API token or password

- [ ] **Step 5: Add `docker/` directory to File Structure and .gitignore**

  ```bash
  mkdir -p docker
  echo "# Docker build contexts for images hosted in dfe-infra" > docker/README.md
  ```

- [ ] **Step 6: Commit**

  ```bash
  git add argocd/values/common.yaml bootstrap/templates/regcred.yaml.tpl \
          bootstrap/bootstrap.sh .github/workflows/docker-build.yml docker/
  git commit -m "feat: add JFrog container registry config (imagePullSecret, CI workflow, registry value)"
  git push
  ```

---

### Task 14: Final Verification

- [ ] **Step 1: Verify repo state on GitHub**

  ```bash
  gh repo view catinspace-au/dfe-infra --web
  ```

  ```bash
  gh repo view catinspace-au/dfe-infra --web
  ```

  Confirm the following are present:
  - `terraform/modules/tf-naming/` with all 3 .tf files + tests/
  - `helm/library/dfe-common/` with Chart.yaml + templates/
  - `argocd/bootstrap/`, `argocd/appsets/`, `argocd/values/` (5 value files: common, local, aws, gcp, azure)
  - `bootstrap/bootstrap.sh` (executable) + `bootstrap/templates/` (3 templates incl. regcred)
  - `.github/workflows/` with 3 CI files (tf-validate, helm-lint, docker-build)
  - `docs/license-review.md`
  - `DIRECTORY.md`
  - `docker/README.md`

---

## Completion Criteria

This plan is complete when:
- [ ] `cd terraform/modules/tf-naming && tofu test` → 10 runs pass, 0 fail
- [ ] `helm lint helm/library/dfe-common/tests/lint-test/` → 0 failures
- [ ] `bash -n bootstrap/bootstrap.sh` → no errors
- [ ] `DFE_DRY_RUN=true bash bootstrap/bootstrap.sh` (with all required vars set including `DFE_REGISTRY_HOST`, `DFE_REGISTRY_USER`, `DFE_REGISTRY_TOKEN`) → prints all steps, no errors
- [ ] `python3 -c "import yaml; ..."` YAML validation passes on all `argocd/**/*.yaml`
- [ ] `argocd/values/` contains 5 files: `common.yaml`, `local.yaml`, `aws.yaml`, `gcp.yaml`, `azure.yaml`
- [ ] `DIRECTORY.md` exists and lists every top-level directory
- [ ] `global.registry` is set in `argocd/values/common.yaml` (replace `<JFROG_HOST>` with actual value)
- [ ] GitHub Actions secrets `JFROG_HOST`, `JFROG_USER`, `JFROG_TOKEN` are configured in repo settings
- [ ] All files committed and pushed to `catinspace-au/dfe-infra` main branch

**Next plan:** `2026-03-30-dfe-infra-02-layer1-rancher.md` — bootstrap.sh fully wired to a real RKE2 cluster, all Layer 1 components (cert-manager, ESO, Envoy Gateway, KEDA) deploying and healthy via ArgoCD.
