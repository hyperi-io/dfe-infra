# DFE Infra 02 — Layer 1 Rancher Bootstrap

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the Terraform modules and local environment that produce all outputs bootstrap.sh needs, then run bootstrap.sh against the real devex RKE2 cluster so ArgoCD manages all Layer 1 components (cert-manager, ESO, Envoy Gateway, KEDA, operators) at waves 2-3.

**Architecture:** Three new TF modules (tf-secrets, tf-iam, tf-storage) for the local/Rancher target. A local environment root module ties them together with tf-naming. A Python bridge script reads `terraform output -json` and exports the env vars bootstrap.sh needs. After bootstrap runs, ArgoCD syncs wave 2-3 ApplicationSets deploying all Layer 1 operators.

**Tech Stack:** Terraform/OpenTofu >=1.6, Vault provider (OpenBao-compatible), Helm 3, Python 3.12 (stdlib only), kubectl, RKE2/Rancher

**Target cluster:** devex.hyperi.io RKE2 (3-node, API VIP 10.66.0.200, OpenBao at bao.devex.hyperi.io:8200)

---

## Prerequisite: What Already Exists

From **Plan 01** (on main):
- `terraform/modules/tf-naming/` — canonical naming module (11 tests passing)
- `bootstrap/bootstrap.sh` — idempotent script with dry-run support
- `bootstrap/templates/` — cluster-secret, ESO ClusterSecretStore, regcred templates
- `argocd/appsets/` — layer1-addons, layer2-data, layer2-apps ApplicationSets
- `argocd/values/common.yaml` + `local.yaml` — Helm value defaults

From **hyperi-infra** (running on devex):
- RKE2 cluster (3 nodes, VIP 10.66.0.200)
- OpenBao at `bao.devex.hyperi.io:8200` (Vault-compatible API)
- NFS storage on `storage.devex.hyperi.io:/data/`
- CoreDNS with `*.apps.devex.hyperi.io → 10.66.0.200`
- cert-manager, ESO, Envoy Gateway, ArgoCD already running (we'll re-bootstrap under dfe-infra management)

---

## File Structure

```
versions.yaml                  # SINGLE SSOT for ALL pinned versions (Helm charts, operators, tools)

terraform/modules/tf-secrets/
├── variables.tf           # vault_addr, project, env, cloud
├── main.tf                # AppRole auth, ESO policy, KV engine setup
├── outputs.tf             # vault_addr, eso_role_id, eso_secret_id
# No unit tests — Vault provider requires live connection. Integration-tested via terraform plan.

terraform/modules/tf-iam/
├── variables.tf           # vault_addr, services list, project, env
├── main.tf                # per-service AppRole + policy in OpenBao
├── outputs.tf             # workload_identity_annotations map (empty for local)
└── tests/
    └── iam.tftest.hcl     # validate per-service role creation

terraform/modules/tf-storage/
├── variables.tf           # cloud, nfs_server, nfs_path, minio_endpoint
├── main.tf                # outputs only for local (NFS/MinIO are pre-existing)
├���─ outputs.tf             # config_storage_type/server/path, backup_endpoint

terraform/environments/local/
├── main.tf                # root module: calls tf-naming, tf-secrets, tf-iam, tf-storage
├── variables.tf           # environment-specific inputs
├── outputs.tf             # all values bootstrap.sh needs, as a flat map
├── terraform.tfvars       # devex-specific values (vault addr, NFS server, etc.)
└── backend.tf             # local backend (state in repo for now; move to remote later)

bootstrap/
├── bridge.py              # Python 3: reads `terraform output -json`, exports env, calls bootstrap.sh
├── bootstrap.sh           # (existing, modify: update chart versions to match hyperi-infra)
└── local.env.example      # example .env file for manual bootstrap (without Terraform)

helm/charts/envoy-gateway-config/
├── Chart.yaml             # GatewayClass + Gateway + ClusterIssuer chart
├── values.yaml            # defaults
└── templates/
    ├── gateway-class.yaml # Envoy GatewayClass
    ├── gateway.yaml       # Gateway with HTTPS listener + externalIPs
    └── cluster-issuer.yaml # cert-manager ClusterIssuer (Let's Encrypt DNS-01)
```

---

## Chunk 0: Version SSOT

### Task 0: Create `versions.yaml` — Single Source of Truth for All Pinned Versions

**Files:**
- Create: `versions.yaml`
- Create: `bootstrap/read_versions.py`

Every pinned version in the repo (Helm charts, operators, tools, providers) lives in ONE file. bootstrap.sh, ArgoCD ApplicationSets, CI workflows, and Dockerfiles all read from it. No version literals scattered across files.

- [ ] **Step 1: Create `versions.yaml`**

  ```yaml
  # versions.yaml — SINGLE SOURCE OF TRUTH for all pinned versions.
  # Every version pin in the repo reads from this file.
  # To upgrade a component: change the version here, commit, push.
  # bootstrap.sh, ArgoCD ApplicationSets, and CI all consume this.

  # Layer 1: Bootstrap components (installed by bootstrap.sh)
  bootstrap:
    cert-manager: "v1.17.2"
    external-secrets: "0.17.0"
    argocd: "7.8.26"
    valkey: "1.0.0"

  # Layer 1: Operators (deployed by ArgoCD wave 2-3)
  operators:
    envoy-gateway: "v1.4.0"
    external-dns: "1.14.3"
    keda: "2.14.0"
    metrics-server: "3.12.0"
    stakater-reloader: "1.1.0"
    cnpg: "0.27.1"
    strimzi-kafka-operator: "0.50.1"
    clickhouse-operator: "0.23.0"

  # Layer 2: Data platform (deployed by ArgoCD wave 4)
  # Versions added in Plan 03
  data: {}

  # Layer 2: DFE apps (deployed by ArgoCD wave 5)
  # Versions added in Plan 06
  apps: {}

  # Terraform providers
  providers:
    hashicorp-null: "~> 3.0"
    hashicorp-vault: "~> 4.0"

  # CI tools
  tools:
    terraform: "1.9.0"
    opentofu: "1.7.0"
    helm: "3.14.0"
  ```

- [ ] **Step 2: Create `bootstrap/read_versions.py`**

  A small Python helper that reads versions.yaml and outputs values. Used by bootstrap.sh and CI.

  ```python
  #!/usr/bin/env python3
  #  Project:      dfe-infra
  #  File:         read_versions.py
  #  Purpose:      Read pinned versions from versions.yaml (the SSOT)
  #  Language:     Python
  #
  #  License:      FSL-1.1-ALv2
  #  Copyright:    (c) 2026 HYPERI PTY LIMITED

  """Read version pins from versions.yaml.

  Usage:
      # Get a single version:
      python3 read_versions.py bootstrap.cert-manager
      # Output: v1.17.2

      # Get all bootstrap versions as KEY=VALUE (for shell eval):
      python3 read_versions.py --section bootstrap --shell
      # Output:
      # CERT_MANAGER_VERSION="v1.17.2"
      # EXTERNAL_SECRETS_VERSION="0.17.0"
      # ARGOCD_VERSION="7.8.26"
      # VALKEY_VERSION="1.0.0"

      # Get as JSON:
      python3 read_versions.py --section operators --json
  """

  import argparse
  import json
  import sys
  from pathlib import Path

  try:
      import yaml
  except ImportError:
      # Fallback: parse the simple YAML ourselves (stdlib only, no PyYAML needed)
      yaml = None


  def load_versions(versions_file: Path) -> dict:
      """Load versions.yaml, with or without PyYAML."""
      text = versions_file.read_text()
      if yaml:
          return yaml.safe_load(text)
      # Minimal YAML parser for our simple flat structure
      return _parse_simple_yaml(text)


  def _parse_simple_yaml(text: str) -> dict:
      """Parse the simple 2-level YAML we use (no nested objects beyond depth 2)."""
      result = {}
      current_section = None
      for line in text.splitlines():
          stripped = line.strip()
          if not stripped or stripped.startswith("#"):
              continue
          if not line.startswith(" ") and stripped.endswith(":"):
              section_name = stripped.rstrip(":").strip()
              # Check for inline value like `data: {}`
              if ":" in stripped and not stripped.endswith(":"):
                  key, val = stripped.split(":", 1)
                  result[key.strip()] = val.strip().strip('"')
              else:
                  current_section = section_name
                  result[current_section] = {}
          elif current_section and ":" in stripped:
              key, val = stripped.split(":", 1)
              key = key.strip()
              val = val.strip().strip('"').strip("'")
              if val == "{}":
                  result[current_section] = {}
              else:
                  result[current_section][key] = val
      return result


  def get_dotpath(data: dict, path: str) -> str:
      """Navigate a.b.c dotpath into nested dict."""
      parts = path.split(".")
      current = data
      for part in parts:
          if not isinstance(current, dict) or part not in current:
              print(f"ERROR: path '{path}' not found in versions.yaml", file=sys.stderr)
              sys.exit(1)
          current = current[part]
      return str(current)


  def main() -> None:
      parser = argparse.ArgumentParser(description="Read versions from versions.yaml")
      parser.add_argument("dotpath", nargs="?", help="Dot-separated path (e.g. bootstrap.cert-manager)")
      parser.add_argument("--section", help="Output all keys in a section")
      parser.add_argument("--shell", action="store_true", help="Output as UPPER_SNAKE=value for shell eval")
      parser.add_argument("--json", action="store_true", help="Output as JSON")
      parser.add_argument("--file", default=None, help="Path to versions.yaml (default: auto-detect)")
      args = parser.parse_args()

      # Find versions.yaml
      if args.file:
          versions_file = Path(args.file)
      else:
          # Walk up from script location to find repo root
          search = Path(__file__).resolve().parent
          while search != search.parent:
              candidate = search / "versions.yaml"
              if candidate.exists():
                  versions_file = candidate
                  break
              search = search.parent
          else:
              print("ERROR: versions.yaml not found", file=sys.stderr)
              sys.exit(1)

      data = load_versions(versions_file)

      if args.dotpath:
          print(get_dotpath(data, args.dotpath))
      elif args.section:
          section = data.get(args.section, {})
          if not isinstance(section, dict):
              print(f"ERROR: section '{args.section}' is not a dict", file=sys.stderr)
              sys.exit(1)
          if args.json:
              print(json.dumps(section, indent=2))
          elif args.shell:
              for k, v in section.items():
                  env_name = k.upper().replace("-", "_") + "_VERSION"
                  print(f'{env_name}="{v}"')
          else:
              for k, v in section.items():
                  print(f"{k}: {v}")
      else:
          if args.json:
              print(json.dumps(data, indent=2))
          else:
              parser.print_help()
              sys.exit(1)


  if __name__ == "__main__":
      main()
  ```

- [ ] **Step 3: Make executable and test**

  ```bash
  chmod +x bootstrap/read_versions.py
  python3 bootstrap/read_versions.py bootstrap.cert-manager
  ```

  Expected: `v1.17.2`

  ```bash
  python3 bootstrap/read_versions.py --section bootstrap --shell
  ```

  Expected:
  ```
  CERT_MANAGER_VERSION="v1.17.2"
  EXTERNAL_SECRETS_VERSION="0.17.0"
  ARGOCD_VERSION="7.8.26"
  VALKEY_VERSION="1.0.0"
  ```

- [ ] **Step 4: Update `bootstrap/bootstrap.sh` to read from versions.yaml**

  Replace hardcoded version strings in bootstrap.sh. Add this block after the TF binary detection, before the required_vars check:

  ```bash
  # Read versions from SSOT
  REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
  CERT_MANAGER_VERSION=$(python3 "${SCRIPT_DIR}/read_versions.py" --file "${REPO_ROOT}/versions.yaml" bootstrap.cert-manager)
  EXTERNAL_SECRETS_VERSION=$(python3 "${SCRIPT_DIR}/read_versions.py" --file "${REPO_ROOT}/versions.yaml" bootstrap.external-secrets)
  ARGOCD_VERSION=$(python3 "${SCRIPT_DIR}/read_versions.py" --file "${REPO_ROOT}/versions.yaml" bootstrap.argocd)
  VALKEY_VERSION=$(python3 "${SCRIPT_DIR}/read_versions.py" --file "${REPO_ROOT}/versions.yaml" bootstrap.valkey)
  ```

  Then replace the hardcoded versions in the helm commands:
  - `--version v1.17.2` → `--version "${CERT_MANAGER_VERSION}"`
  - `--version 0.17.0` → `--version "${EXTERNAL_SECRETS_VERSION}"`
  - `--version 7.8.26` → `--version "${ARGOCD_VERSION}"`
  - `--version 1.0.0` → `--version "${VALKEY_VERSION}"`

- [ ] **Step 5: Update `argocd/appsets/layer1-addons.yaml` to document version source**

  Add a comment at the top of layer1-addons.yaml:

  ```yaml
  # IMPORTANT: Chart versions in this file MUST match versions.yaml (the SSOT).
  # When updating versions, change versions.yaml first, then update this file to match.
  # TODO(Plan 03+): Automate this — generate ApplicationSet elements from versions.yaml via a Python script.
  ```

  For now, the ApplicationSet versions are manually kept in sync with versions.yaml. Automation (generating the ApplicationSet from versions.yaml) is deferred — it requires either a pre-commit hook or a CI step that renders the ApplicationSet template. Keep it manual for Plan 02 since the component list is stable.

- [ ] **Step 6: Verify bootstrap.sh still works with dynamic versions**

  ```bash
  bash -n bootstrap/bootstrap.sh && echo "Syntax OK"
  ```

  Then dry-run:
  ```bash
  export DFE_ENV=local DFE_CLOUD=local DFE_REGION=local
  export DFE_DOMAIN=devex.hyperi.io DFE_TENANCY=dev
  export DFE_REPO_URL=https://github.com/catinspace-au/dfe-infra DFE_TARGET_REVISION=main
  export DFE_STORAGE_CLASS=local-path DFE_NAMESPACE=dfe-local
  export DFE_CLICKHOUSE_HOST=ch DFE_KAFKA_BOOTSTRAP=kafka DFE_OTEL_ENDPOINT=otel
  export DFE_VAULT_ADDR=https://bao.devex.hyperi.io:8200 DFE_VAULT_ROLE_ID=test
  export DFE_WORKLOAD_IDENTITY_ANNOTATIONS='{}' DFE_REGISTRY_HOST=test DFE_REGISTRY_USER=test DFE_REGISTRY_TOKEN=test
  export DFE_DRY_RUN=true
  bash bootstrap/bootstrap.sh
  ```

  Expected: version strings from versions.yaml appear in the `[DRY-RUN]` helm commands.

- [ ] **Step 7: Commit**

  ```bash
  git add versions.yaml bootstrap/read_versions.py bootstrap/bootstrap.sh argocd/appsets/layer1-addons.yaml
  git commit -m "feat: add versions.yaml SSOT — all pinned versions in one file"
  ```

---

## Chunk 1: Terraform Modules (tf-secrets, tf-iam, tf-storage)

### Task 1: tf-secrets Module — Write Failing Tests First

**Files:**
- Create: `terraform/modules/tf-secrets/tests/secrets.tftest.hcl`

- [ ] **Step 1: Note on testing approach**

  The Vault provider requires a live connection even in `plan` mode — mock tokens/addresses won't work. Therefore:
  - **Unit testing:** `terraform validate` only (syntax + type checking, no provider connection)
  - **Integration testing:** `terraform plan` against the real devex OpenBao instance (run manually or in CI with `VAULT_ADDR` + `VAULT_TOKEN` set)

  No `.tftest.hcl` file for tf-secrets — the Vault provider makes plan-only unit tests impractical. Validation is via `terraform validate` + live `terraform plan` against devex.

---

### Task 2: tf-secrets Module — Implement

**Files:**
- Create: `terraform/modules/tf-secrets/variables.tf`
- Create: `terraform/modules/tf-secrets/main.tf`
- Create: `terraform/modules/tf-secrets/outputs.tf`

- [ ] **Step 1: Write `variables.tf`**

  ```hcl
  # terraform/modules/tf-secrets/variables.tf

  terraform {
    required_providers {
      vault = {
        source  = "hashicorp/vault"
        version = "~> 4.0"
      }
    }
  }

  variable "vault_addr" {
    description = "OpenBao/Vault server address (e.g. https://bao.devex.hyperi.io:8200)"
    type        = string
  }

  variable "project" {
    description = "Project identifier (from tf-naming)"
    type        = string
    default     = "dfe"
  }

  variable "env" {
    description = "Deployment environment"
    type        = string
  }

  variable "cloud" {
    description = "Target cloud (local/aws/gcp/az)"
    type        = string
  }
  ```

- [ ] **Step 2: Write `main.tf`**

  ```hcl
  # terraform/modules/tf-secrets/main.tf
  #
  # Configures the secrets backend for ESO integration.
  # For local/Rancher: OpenBao with AppRole auth.
  # For cloud targets: this module is skipped (cloud SM configured separately).

  # Enable KV v2 secrets engine for DFE at secret/
  # (idempotent — Vault ignores if already enabled)
  resource "vault_mount" "dfe_kv" {
    path        = "secret"
    type        = "kv-v2"
    description = "DFE KV secrets engine"

    # Don't destroy the mount if we remove from TF
    lifecycle {
      prevent_destroy = true
    }
  }

  # Enable AppRole auth method
  resource "vault_auth_backend" "approle" {
    type = "approle"
    path = "approle"
  }

  # ESO needs read access to all DFE secrets
  resource "vault_policy" "eso" {
    name   = "${var.project}-eso-${var.env}"
    policy = <<-EOT
      # ESO read-only access to DFE secrets
      path "secret/data/${var.project}/${var.env}/*" {
        capabilities = ["read", "list"]
      }
      path "secret/metadata/${var.project}/${var.env}/*" {
        capabilities = ["read", "list"]
      }
    EOT
  }

  # AppRole for ESO to authenticate
  resource "vault_approle_auth_backend_role" "eso" {
    backend   = vault_auth_backend.approle.path
    role_name = "${var.project}-eso-${var.env}"

    token_policies  = [vault_policy.eso.name]
    token_ttl       = 3600
    token_max_ttl   = 86400
    token_num_uses  = 0 # unlimited
  }

  # Generate a SecretID for the ESO AppRole
  resource "vault_approle_auth_backend_role_secret_id" "eso" {
    backend   = vault_auth_backend.approle.path
    role_name = vault_approle_auth_backend_role.eso.role_name
  }

  # Seed initial secrets (empty placeholders — operators fill real values)
  resource "vault_kv_secret_v2" "seed_argocd" {
    mount = vault_mount.dfe_kv.path
    name  = "${var.project}/${var.env}/argocd"
    data_json = jsonencode({
      admin_password = ""
    })

    lifecycle {
      ignore_changes = [data_json] # don't overwrite if manually updated
    }
  }
  ```

- [ ] **Step 3: Write `outputs.tf`**

  ```hcl
  # terraform/modules/tf-secrets/outputs.tf

  output "vault_addr" {
    description = "OpenBao/Vault server address"
    value       = var.vault_addr
  }

  output "eso_role_id" {
    description = "AppRole role_id for ESO to authenticate with OpenBao"
    value       = vault_approle_auth_backend_role.eso.role_id
  }

  output "eso_secret_id" {
    description = "AppRole secret_id for ESO (sensitive)"
    value       = vault_approle_auth_backend_role_secret_id.eso.secret_id
    sensitive   = true
  }

  output "eso_policy_name" {
    description = "Vault policy name assigned to ESO AppRole"
    value       = vault_policy.eso.name
  }

  output "kv_mount_path" {
    description = "KV v2 secrets engine mount path"
    value       = vault_mount.dfe_kv.path
  }
  ```

- [ ] **Step 4: Run `terraform init` and `terraform validate`**

  ```bash
  cd terraform/modules/tf-secrets
  terraform init
  terraform validate
  ```

  Expected: `Success! The configuration is valid.`

- [ ] **Step 5: Commit**

  ```bash
  git add terraform/modules/tf-secrets/
  git commit -m "feat: add tf-secrets module (OpenBao AppRole for ESO, KV engine)"
  ```

---

### Task 3: tf-iam Module ��� Implement

**Files:**
- Create: `terraform/modules/tf-iam/variables.tf`
- Create: `terraform/modules/tf-iam/main.tf`
- Create: `terraform/modules/tf-iam/outputs.tf`

The tf-iam module creates per-service AppRoles in OpenBao for the local target. For cloud targets (AWS/GCP/Azure), this module creates IRSA roles / GCP WIF bindings / Azure MI instead. Plan 02 only implements the local variant.

- [ ] **Step 1: Write `variables.tf`**

  ```hcl
  # terraform/modules/tf-iam/variables.tf

  terraform {
    required_providers {
      vault = {
        source  = "hashicorp/vault"
        version = "~> 4.0"
      }
    }
  }

  variable "project" {
    description = "Project identifier (from tf-naming)"
    type        = string
    default     = "dfe"
  }

  variable "env" {
    description = "Deployment environment"
    type        = string
  }

  variable "cloud" {
    description = "Target cloud platform"
    type        = string
  }

  variable "services" {
    description = "List of DFE service names that need workload identity (AppRole per service)"
    type        = list(string)
    default     = ["engine", "ui", "receiver", "loader", "archiver", "fetcher"]
  }

  variable "vault_approle_backend_path" {
    description = "AppRole auth backend path (created by tf-secrets)"
    type        = string
    default     = "approle"
  }

  variable "kv_mount_path" {
    description = "KV v2 mount path (created by tf-secrets)"
    type        = string
    default     = "secret"
  }
  ```

- [ ] **Step 2: Write `main.tf`**

  ```hcl
  # terraform/modules/tf-iam/main.tf
  #
  # Creates per-service AppRoles in OpenBao (local target).
  # Each service gets a scoped policy: read-only to its own secret path.
  # Cloud targets (aws/gcp/az) replace this with IRSA/WIF/Azure MI — not yet implemented.

  module "naming" {
    source = "../tf-naming"

    for_each  = toset(var.services)
    project   = var.project
    component = each.key
    env       = var.env
    cloud     = var.cloud
  }

  # Per-service policy: read-only to service-specific secrets
  resource "vault_policy" "service" {
    for_each = toset(var.services)

    name   = module.naming[each.key].vault_policy_name
    policy = <<-EOT
      path "${var.kv_mount_path}/data/${var.project}/${var.env}/${each.key}/*" {
        capabilities = ["read", "list"]
      }
      path "${var.kv_mount_path}/metadata/${var.project}/${var.env}/${each.key}/*" {
        capabilities = ["read", "list"]
      }
    EOT
  }

  # Per-service AppRole
  resource "vault_approle_auth_backend_role" "service" {
    for_each = toset(var.services)

    backend   = var.vault_approle_backend_path
    role_name = module.naming[each.key].vault_approle_name

    token_policies  = [vault_policy.service[each.key].name]
    token_ttl       = 3600
    token_max_ttl   = 86400
  }
  ```

- [ ] **Step 3: Write `outputs.tf`**

  ```hcl
  # terraform/modules/tf-iam/outputs.tf

  # For local target: workload_identity_annotations is empty (OpenBao uses AppRole, not K8s SA annotations).
  # For cloud targets: this would contain {"eks.amazonaws.com/role-arn": "..."} etc.
  output "workload_identity_annotations" {
    description = "Map of service → K8s ServiceAccount annotations for workload identity. Empty for local (AppRole-based)."
    value = {
      for svc in var.services : svc => {}
    }
  }

  output "service_approle_names" {
    description = "Map of service → AppRole name in OpenBao"
    value = {
      for svc in var.services : svc => vault_approle_auth_backend_role.service[svc].role_name
    }
  }

  output "service_policy_names" {
    description = "Map of service → Vault policy name"
    value = {
      for svc in var.services : svc => vault_policy.service[svc].name
    }
  }
  ```

- [ ] **Step 4: Write basic test `tests/iam.tftest.hcl`**

  ```hcl
  # terraform/modules/tf-iam/tests/iam.tftest.hcl
  # Integration test — requires a live Vault/OpenBao instance.
  # Run with: VAULT_ADDR=... VAULT_TOKEN=... terraform test
  # CI: skipped unless VAULT_ADDR is set.

  variables {
    project   = "dfe"
    env       = "test"
    cloud     = "local"
    services  = ["loader", "receiver"]
  }

  run "per_service_outputs" {
    command = plan

    assert {
      condition     = length(output.service_approle_names) == 2
      error_message = "should create AppRole per service (expected 2)"
    }

    assert {
      condition     = output.service_approle_names["loader"] == "dfe-loader-test"
      error_message = "loader AppRole name should be dfe-loader-test"
    }

    assert {
      condition     = length(output.workload_identity_annotations) == 2
      error_message = "should output workload_identity_annotations per service"
    }
  }
  ```

  Note: This test requires a live Vault connection. Run only when `VAULT_ADDR` is set.

- [ ] **Step 5: Validate**

  ```bash
  cd terraform/modules/tf-iam
  terraform init
  terraform validate
  ```

- [ ] **Step 6: Commit**

  ```bash
  git add terraform/modules/tf-iam/
  git commit -m "feat: add tf-iam module (per-service OpenBao AppRole for local target)"
  ```

---

### Task 4: tf-storage Module — Implement

**Files:**
- Create: `terraform/modules/tf-storage/variables.tf`
- Create: `terraform/modules/tf-storage/main.tf`
- Create: `terraform/modules/tf-storage/outputs.tf`

For local, storage is pre-existing (NFS, MinIO). The module just outputs the endpoints — no resources created.

- [ ] **Step 1: Write `variables.tf`**

  ```hcl
  # terraform/modules/tf-storage/variables.tf

  variable "cloud" {
    description = "Target cloud (local/aws/gcp/az)"
    type        = string
  }

  variable "nfs_server" {
    description = "NFS server hostname (local target)"
    type        = string
    default     = ""
  }

  variable "nfs_config_path" {
    description = "NFS export path for DFE config storage"
    type        = string
    default     = "/data/dfe-config"
  }

  variable "nfs_backup_path" {
    description = "NFS export path for backups"
    type        = string
    default     = "/data/dfe-backups"
  }

  variable "minio_endpoint" {
    description = "MinIO S3-compatible endpoint (local target)"
    type        = string
    default     = ""
  }
  ```

- [ ] **Step 2: Write `main.tf` and `outputs.tf`**

  ```hcl
  # terraform/modules/tf-storage/main.tf
  # For local: no resources created — NFS and MinIO are pre-existing.
  # For cloud: this would create S3/GCS/Azure Blob buckets (not yet implemented).

  locals {
    config_storage = {
      local = {
        type   = "nfs"
        server = var.nfs_server
        path   = var.nfs_config_path
      }
      aws = {
        type   = "s3"
        server = ""
        path   = ""
      }
      gcp = {
        type   = "gcs"
        server = ""
        path   = ""
      }
      az = {
        type   = "azurefiles"
        server = ""
        path   = ""
      }
    }
  }
  ```

  ```hcl
  # terraform/modules/tf-storage/outputs.tf

  output "config_storage_type" {
    description = "Config storage backend type (nfs/s3/gcs/azurefiles)"
    value       = local.config_storage[var.cloud].type
  }

  output "config_storage_server" {
    description = "Config storage server (NFS hostname or empty for cloud)"
    value       = local.config_storage[var.cloud].server
  }

  output "config_storage_path" {
    description = "Config storage path (NFS export path or empty for cloud)"
    value       = local.config_storage[var.cloud].path
  }

  output "backup_endpoint" {
    description = "Backup storage endpoint (MinIO for local, S3/GCS/Azure for cloud)"
    value       = var.cloud == "local" ? var.minio_endpoint : ""
  }
  ```

- [ ] **Step 3: Write test `tests/storage.tftest.hcl`**

  ```hcl
  # terraform/modules/tf-storage/tests/storage.tftest.hcl
  # Pure unit test — no providers, no external connections.

  variables {
    cloud          = "local"
    nfs_server     = "storage.devex.hyperi.io"
    nfs_config_path = "/data/dfe-config"
  }

  run "local_config_storage" {
    command = plan

    assert {
      condition     = output.config_storage_type == "nfs"
      error_message = "local cloud should use nfs config storage"
    }

    assert {
      condition     = output.config_storage_server == "storage.devex.hyperi.io"
      error_message = "config_storage_server should match nfs_server input"
    }
  }

  run "aws_config_storage" {
    command = plan

    variables {
      cloud = "aws"
    }

    assert {
      condition     = output.config_storage_type == "s3"
      error_message = "aws cloud should use s3 config storage"
    }
  }
  ```

- [ ] **Step 4: Validate and run test**

  ```bash
  cd terraform/modules/tf-storage
  terraform init
  terraform validate
  terraform test
  ```

  Expected: 2 runs pass (no provider needed — pure locals).

- [ ] **Step 5: Commit**

  ```bash
  git add terraform/modules/tf-storage/
  git commit -m "feat: add tf-storage module (NFS/MinIO outputs for local target)"
  ```

---

## Chunk 2: Local Environment + TF-to-Bootstrap Bridge

### Task 5: Local Terraform Environment

**Files:**
- Create: `terraform/environments/local/main.tf`
- Create: `terraform/environments/local/variables.tf`
- Create: `terraform/environments/local/outputs.tf`
- Create: `terraform/environments/local/terraform.tfvars`
- Create: `terraform/environments/local/backend.tf`

This is the root module that ties tf-naming, tf-secrets, tf-iam, and tf-storage together for the devex Rancher cluster. Its outputs are everything bootstrap.sh needs.

- [ ] **Step 1: Write `backend.tf`**

  ```hcl
  # terraform/environments/local/backend.tf
  # Local state for now. Move to remote (S3/MinIO) when mature.
  terraform {
    backend "local" {
      path = "terraform.tfstate"
    }
  }
  ```

- [ ] **Step 2: Write `variables.tf`**

  ```hcl
  # terraform/environments/local/variables.tf

  variable "vault_addr" {
    description = "OpenBao server address"
    type        = string
  }

  variable "vault_token" {
    description = "OpenBao root/admin token for Terraform provisioning (sensitive)"
    type        = string
    sensitive   = true
  }

  variable "domain" {
    description = "Base domain for services (e.g. devex.hyperi.io)"
    type        = string
  }

  variable "nfs_server" {
    description = "NFS server hostname"
    type        = string
  }

  variable "minio_endpoint" {
    description = "MinIO S3-compatible endpoint"
    type        = string
    default     = ""
  }

  variable "repo_url" {
    description = "Git repo URL for ArgoCD"
    type        = string
  }

  variable "target_revision" {
    description = "Git branch/tag for ArgoCD"
    type        = string
    default     = "main"
  }

  variable "registry_host" {
    description = "Container registry hostname (JFrog)"
    type        = string
    default     = ""
  }

  variable "registry_user" {
    description = "Container registry username"
    type        = string
    default     = ""
  }

  variable "registry_token" {
    description = "Container registry token (sensitive)"
    type        = string
    sensitive   = true
    default     = ""
  }
  ```

- [ ] **Step 3: Write `main.tf`**

  ```hcl
  # terraform/environments/local/main.tf
  # Root module for the local (Rancher/RKE2) deployment target.
  # Calls tf-naming, tf-secrets, tf-iam, tf-storage and wires them together.

  terraform {
    required_version = ">= 1.6.0"

    required_providers {
      vault = {
        source  = "hashicorp/vault"
        version = "~> 4.0"
      }
    }
  }

  provider "vault" {
    address = var.vault_addr
    token   = var.vault_token
  }

  locals {
    project = "dfe"
    env     = "local"
    cloud   = "local"
    region  = "local"
    tenancy = "dev"
  }

  # Naming for the cluster-level identity
  module "naming" {
    source = "../../modules/tf-naming"

    project   = local.project
    component = "cluster"
    env       = local.env
    cloud     = local.cloud
    region    = local.region
  }

  # Secrets backend: OpenBao AppRole for ESO
  module "secrets" {
    source = "../../modules/tf-secrets"

    vault_addr = var.vault_addr
    project    = local.project
    env        = local.env
    cloud      = local.cloud
  }

  # Per-service workload identity: OpenBao AppRoles
  module "iam" {
    source = "../../modules/tf-iam"

    project                    = local.project
    env                        = local.env
    cloud                      = local.cloud
    vault_approle_backend_path = "approle"
    kv_mount_path              = module.secrets.kv_mount_path

    depends_on = [module.secrets]
  }

  # Storage configuration (NFS for local)
  module "storage" {
    source = "../../modules/tf-storage"

    cloud          = local.cloud
    nfs_server     = var.nfs_server
    minio_endpoint = var.minio_endpoint
  }
  ```

- [ ] **Step 4: Write `outputs.tf`**

  All outputs map 1:1 to bootstrap.sh required env vars.

  ```hcl
  # terraform/environments/local/outputs.tf
  # Every output here maps directly to a DFE_* env var consumed by bootstrap.sh.
  # The bridge.py script reads these via `terraform output -json`.

  output "DFE_ENV" {
    value = local.env
  }

  output "DFE_CLOUD" {
    value = local.cloud
  }

  output "DFE_REGION" {
    value = local.region
  }

  output "DFE_DOMAIN" {
    value = var.domain
  }

  output "DFE_TENANCY" {
    value = local.tenancy
  }

  output "DFE_REPO_URL" {
    value = var.repo_url
  }

  output "DFE_TARGET_REVISION" {
    value = var.target_revision
  }

  output "DFE_STORAGE_CLASS" {
    value = "local-path"
  }

  output "DFE_NAMESPACE" {
    value = module.naming.k8s_namespace
  }

  output "DFE_CLICKHOUSE_HOST" {
    value = "clickhouse.clickhouse.svc.cluster.local"
  }

  output "DFE_KAFKA_BOOTSTRAP" {
    value = "dfe-kafka-kafka-bootstrap.strimzi.svc.cluster.local:9092"
  }

  output "DFE_OTEL_ENDPOINT" {
    value = "otel-collector-gateway.otel.svc.cluster.local:4317"
  }

  output "DFE_VAULT_ADDR" {
    value = module.secrets.vault_addr
  }

  output "DFE_VAULT_ROLE_ID" {
    value     = module.secrets.eso_role_id
    sensitive = true
  }

  output "DFE_WORKLOAD_IDENTITY_ANNOTATIONS" {
    value = jsonencode(module.iam.workload_identity_annotations)
  }

  output "DFE_REGISTRY_HOST" {
    value = var.registry_host
  }

  output "DFE_REGISTRY_USER" {
    value = var.registry_user
  }

  output "DFE_REGISTRY_TOKEN" {
    value     = var.registry_token
    sensitive = true
  }
  ```

- [ ] **Step 5: Write `terraform.tfvars`**

  ```hcl
  # terraform/environments/local/terraform.tfvars
  # Devex cluster-specific values. Non-sensitive only — vault_token via env var.

  vault_addr      = "https://bao.devex.hyperi.io:8200"
  domain          = "devex.hyperi.io"
  nfs_server      = "storage.devex.hyperi.io"
  minio_endpoint  = "https://minio.devex.hyperi.io"
  repo_url        = "https://github.com/catinspace-au/dfe-infra"
  target_revision = "main"
  ```

- [ ] **Step 6: Validate**

  ```bash
  cd terraform/environments/local
  terraform init
  terraform validate
  ```

- [ ] **Step 7: Commit**

  ```bash
  git add terraform/environments/local/
  git commit -m "feat: add local Terraform environment (devex RKE2 target)"
  ```

---

### Task 6: Python Bridge Script (TF outputs → bootstrap.sh)

**Files:**
- Create: `bootstrap/bridge.py`
- Create: `bootstrap/local.env.example`

Per standards: bash >20 lines with JSON processing → use Python 3 + stdlib.

- [ ] **Step 1: Write `bootstrap/bridge.py`**

  ```python
  #!/usr/bin/env python3
  #  Project:      dfe-infra
  #  File:         bridge.py
  #  Purpose:      Read Terraform outputs and invoke bootstrap.sh with correct env vars
  #  Language:     Python
  #
  #  License:      FSL-1.1-ALv2
  #  Copyright:    (c) 2026 HYPERI PTY LIMITED

  """Bridge from Terraform outputs to bootstrap.sh environment variables.

  Usage:
      # From terraform/environments/local/ after `terraform apply`:
      python3 ../../../bootstrap/bridge.py

      # Or specify the Terraform directory:
      python3 bootstrap/bridge.py --tf-dir terraform/environments/local

      # Dry-run (print env vars, don't execute bootstrap):
      python3 bootstrap/bridge.py --tf-dir terraform/environments/local --dry-run
  """

  import argparse
  import json
  import os
  import subprocess
  import sys
  from pathlib import Path


  def get_tf_outputs(tf_dir: str) -> dict[str, str]:
      """Run `terraform output -json` and return a flat dict of name→value.

      Handles sensitive outputs: `terraform output -json` redacts sensitive values.
      For any output marked sensitive, we fall back to `terraform output -raw <key>`.
      """
      tf_bin = _find_tf_binary()
      result = subprocess.run(
          [tf_bin, "output", "-json"],
          cwd=tf_dir,
          capture_output=True,
          text=True,
      )
      if result.returncode != 0:
          print(f"ERROR: {tf_bin} output failed:\n{result.stderr}", file=sys.stderr)
          sys.exit(1)

      raw = json.loads(result.stdout)
      outputs = {}
      for k, v in raw.items():
          if v.get("sensitive", False):
              # Sensitive outputs are redacted in -json mode; fetch individually
              raw_result = subprocess.run(
                  [tf_bin, "output", "-raw", k],
                  cwd=tf_dir,
                  capture_output=True,
                  text=True,
              )
              if raw_result.returncode != 0:
                  print(f"WARNING: could not read sensitive output '{k}': {raw_result.stderr}", file=sys.stderr)
                  outputs[k] = ""
              else:
                  outputs[k] = raw_result.stdout.strip()
          else:
              outputs[k] = str(v["value"])
      return outputs


  def _find_tf_binary() -> str:
      """Find terraform or tofu binary, exit if neither found."""
      from shutil import which
      for cmd in ("tofu", "terraform"):
          if which(cmd):
              return cmd
      print("ERROR: neither terraform nor opentofu found on PATH", file=sys.stderr)
      sys.exit(1)


  def _which(cmd: str) -> bool:
      """Check if a command exists on PATH."""
      from shutil import which
      return which(cmd) is not None


  def main() -> None:
      parser = argparse.ArgumentParser(description="Bridge Terraform outputs to bootstrap.sh")
      parser.add_argument(
          "--tf-dir",
          default="terraform/environments/local",
          help="Path to Terraform environment directory (default: terraform/environments/local)",
      )
      parser.add_argument(
          "--dry-run",
          action="store_true",
          help="Print env vars and bootstrap command without executing",
      )
      parser.add_argument(
          "--bootstrap-args",
          nargs="*",
          default=[],
          help="Extra arguments to pass to bootstrap.sh",
      )
      args = parser.parse_args()

      # Resolve paths
      repo_root = Path(__file__).resolve().parent.parent
      tf_dir = (repo_root / args.tf_dir).resolve()
      bootstrap_sh = repo_root / "bootstrap" / "bootstrap.sh"

      if not tf_dir.is_dir():
          print(f"ERROR: Terraform directory not found: {tf_dir}", file=sys.stderr)
          sys.exit(1)

      # Read Terraform outputs
      print(f"Reading Terraform outputs from {tf_dir}...")
      outputs = get_tf_outputs(str(tf_dir))

      # Filter to DFE_* keys only (TF outputs are named to match env vars)
      env_vars = {k: v for k, v in outputs.items() if k.startswith("DFE_")}

      if not env_vars:
          print("ERROR: No DFE_* outputs found in Terraform state.", file=sys.stderr)
          print("Did you run `terraform apply` first?", file=sys.stderr)
          sys.exit(1)

      # Validate required vars match bootstrap.sh expectations
      required = {
          "DFE_ENV", "DFE_CLOUD", "DFE_REGION", "DFE_DOMAIN", "DFE_TENANCY",
          "DFE_REPO_URL", "DFE_TARGET_REVISION",
          "DFE_STORAGE_CLASS", "DFE_NAMESPACE",
          "DFE_CLICKHOUSE_HOST", "DFE_KAFKA_BOOTSTRAP", "DFE_OTEL_ENDPOINT",
          "DFE_VAULT_ADDR", "DFE_VAULT_ROLE_ID",
          "DFE_WORKLOAD_IDENTITY_ANNOTATIONS",
      }
      missing = required - set(env_vars.keys())
      if missing:
          print(f"ERROR: Missing required outputs: {', '.join(sorted(missing))}", file=sys.stderr)
          sys.exit(1)

      if args.dry_run:
          print("\n--- Environment Variables (from Terraform) ---")
          for k in sorted(env_vars.keys()):
              v = env_vars[k]
              # Mask sensitive values
              if "TOKEN" in k or "SECRET" in k or "ROLE_ID" in k:
                  v = v[:4] + "***" if len(v) > 4 else "***"
              print(f"  {k}={v}")
          print(f"\nWould run: DFE_DRY_RUN=true bash {bootstrap_sh}")
          env_vars["DFE_DRY_RUN"] = "true"

      # Merge with current env (bootstrap.sh may need PATH, HOME, etc.)
      full_env = {**os.environ, **env_vars}

      if args.dry_run:
          full_env["DFE_DRY_RUN"] = "true"

      # Execute bootstrap.sh
      print(f"\nExecuting {bootstrap_sh}...")
      result = subprocess.run(
          ["bash", str(bootstrap_sh)] + args.bootstrap_args,
          env=full_env,
      )
      sys.exit(result.returncode)


  if __name__ == "__main__":
      main()
  ```

- [ ] **Step 2: Make executable and test syntax**

  ```bash
  chmod +x bootstrap/bridge.py
  python3 -c "import ast; ast.parse(open('bootstrap/bridge.py').read())" && echo "OK"
  ```

- [ ] **Step 3: Write `bootstrap/local.env.example`**

  ```bash
  # bootstrap/local.env.example
  # Manual bootstrap without Terraform. Copy to .env and fill values.
  # Prefer using bridge.py with Terraform outputs instead.

  DFE_ENV="local"
  DFE_CLOUD="local"
  DFE_REGION="local"
  DFE_DOMAIN="devex.hyperi.io"
  DFE_TENANCY="dev"
  DFE_REPO_URL="https://github.com/catinspace-au/dfe-infra"
  DFE_TARGET_REVISION="main"
  DFE_STORAGE_CLASS="local-path"
  DFE_NAMESPACE="dfe-local"
  DFE_CLICKHOUSE_HOST="clickhouse.clickhouse.svc.cluster.local"
  DFE_KAFKA_BOOTSTRAP="dfe-kafka-kafka-bootstrap.strimzi.svc.cluster.local:9092"
  DFE_OTEL_ENDPOINT="otel-collector-gateway.otel.svc.cluster.local:4317"
  DFE_VAULT_ADDR="https://bao.devex.hyperi.io:8200"
  DFE_VAULT_ROLE_ID=""
  DFE_WORKLOAD_IDENTITY_ANNOTATIONS="{}"
  DFE_REGISTRY_HOST=""
  DFE_REGISTRY_USER=""
  DFE_REGISTRY_TOKEN=""
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add bootstrap/bridge.py bootstrap/local.env.example
  git commit -m "feat: add Python bridge script (TF outputs → bootstrap.sh env vars)"
  ```

---

## Chunk 3: Envoy Gateway Config Chart + Verification

### Task 7: Envoy Gateway Config Helm Chart

**Files:**
- Create: `helm/charts/envoy-gateway-config/Chart.yaml`
- Create: `helm/charts/envoy-gateway-config/values.yaml`
- Create: `helm/charts/envoy-gateway-config/templates/gateway-class.yaml`
- Create: `helm/charts/envoy-gateway-config/templates/gateway.yaml`
- Create: `helm/charts/envoy-gateway-config/templates/cluster-issuer.yaml`

This chart creates the GatewayClass, Gateway, and cert-manager ClusterIssuer resources that Envoy Gateway needs. It's deployed by ArgoCD as part of wave 2.

- [ ] **Step 1: Create `Chart.yaml`**

  ```yaml
  apiVersion: v2
  name: envoy-gateway-config
  description: GatewayClass, Gateway, and ClusterIssuer for Envoy Gateway ingress
  type: application
  version: 0.1.0
  appVersion: "1.0.0"
  dependencies:
    - name: dfe-common
      version: "0.1.0"
      repository: "file://../../library/dfe-common"
  ```

- [ ] **Step 2: Create `values.yaml`**

  ```yaml
  # Default values for envoy-gateway-config
  project: dfe
  component: gateway
  env: local
  cloud: local

  gateway:
    name: dfe-gateway
    namespace: envoy-gateway-system
    # For local: externalIPs mode. For cloud: LoadBalancer with cloud annotations.
    listenerPort: 443
    # externalIPs for local Rancher (set via ArgoCD values overlay)
    externalIPs: []

  tls:
    # cert-manager ClusterIssuer
    issuerName: dfe-letsencrypt
    # Cloudflare DNS-01 solver (Let's Encrypt)
    acme:
      email: ""
      server: "https://acme-v02.api.letsencrypt.org/directory"
      # Cloudflare API token secret (created by ESO)
      cloudflareTokenSecretName: cloudflare-api-token
      cloudflareTokenSecretKey: token

  domain: ""
  ```

- [ ] **Step 3: Create `templates/gateway-class.yaml`**

  ```yaml
  apiVersion: gateway.networking.k8s.io/v1
  kind: GatewayClass
  metadata:
    name: dfe-envoy
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    controllerName: gateway.envoyproxy.io/gatewayclass-controller
  ```

- [ ] **Step 4: Create `templates/gateway.yaml`**

  ```yaml
  apiVersion: gateway.networking.k8s.io/v1
  kind: Gateway
  metadata:
    name: {{ .Values.gateway.name }}
    namespace: {{ .Values.gateway.namespace }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
    annotations:
      cert-manager.io/cluster-issuer: {{ .Values.tls.issuerName }}
  spec:
    gatewayClassName: dfe-envoy
    listeners:
      - name: https
        protocol: HTTPS
        port: {{ .Values.gateway.listenerPort }}
        hostname: "*.{{ .Values.domain }}"
        tls:
          mode: Terminate
          certificateRefs:
            - name: dfe-wildcard-tls
              kind: Secret
        allowedRoutes:
          namespaces:
            from: All
      - name: http
        protocol: HTTP
        port: 80
        hostname: "*.{{ .Values.domain }}"
        allowedRoutes:
          namespaces:
            from: All
  ```

- [ ] **Step 5: Create `templates/cluster-issuer.yaml`**

  ```yaml
  {{- if .Values.tls.acme.email }}
  apiVersion: cert-manager.io/v1
  kind: ClusterIssuer
  metadata:
    name: {{ .Values.tls.issuerName }}
    labels:
      {{- include "dfe-common.labels" . | nindent 4 }}
  spec:
    acme:
      email: {{ .Values.tls.acme.email }}
      server: {{ .Values.tls.acme.server }}
      privateKeySecretRef:
        name: dfe-letsencrypt-account-key
      solvers:
        - dns01:
            cloudflare:
              apiTokenSecretRef:
                name: {{ .Values.tls.acme.cloudflareTokenSecretName }}
                key: {{ .Values.tls.acme.cloudflareTokenSecretKey }}
  {{- end }}
  ```

- [ ] **Step 6: Lint the chart**

  ```bash
  cd helm/charts/envoy-gateway-config
  helm dependency update
  helm lint .
  ```

- [ ] **Step 7: Commit**

  ```bash
  git add helm/charts/envoy-gateway-config/
  git commit -m "feat: add envoy-gateway-config chart (GatewayClass, Gateway, ClusterIssuer)"
  ```

---

### Task 8: Align layer1-addons.yaml versions with versions.yaml

**Files:**
- Modify: `argocd/appsets/layer1-addons.yaml` — update chart version strings to match versions.yaml

bootstrap.sh was already updated in Task 0 to read from versions.yaml dynamically. The ApplicationSet still has hardcoded version strings from Plan 01 that need updating to match. (Automation deferred — for now, manual sync.)

- [ ] **Step 1: Update ALL layer1-addons.yaml chart versions to match versions.yaml**

  In `argocd/appsets/layer1-addons.yaml`, update every `version` field to match `versions.yaml`:

  | Chart | Old (Plan 01) | New (versions.yaml) | Changed? |
  |-------|---------------|---------------------|----------|
  | cert-manager | `v1.14.0` | `v1.17.2` | Yes |
  | external-secrets | `0.9.13` | `0.17.0` | Yes |
  | envoy-gateway | `v1.2.0` | `v1.4.0` | Yes |
  | external-dns | `1.14.3` | `1.14.3` | No (already correct) |
  | keda | `2.14.0` | `2.14.0` | No (already correct) |
  | metrics-server | `3.12.0` | `3.12.0` | No (already correct) |
  | stakater-reloader | `1.1.0` | `1.1.0` | No (already correct) |
  | cnpg | `0.21.0` | `0.27.1` | Yes |
  | strimzi-kafka-operator | `0.40.0` | `0.50.1` | Yes |
  | clickhouse-operator | `0.23.0` | `0.23.0` | No (already correct) |

- [ ] **Step 2: Cross-check with versions.yaml**

  ```bash
  python3 bootstrap/read_versions.py --section operators
  python3 bootstrap/read_versions.py --section bootstrap
  ```

  Verify every version in layer1-addons.yaml and bootstrap.sh matches the output.

- [ ] **Step 3: Validate**

  ```bash
  python3 -c "import yaml; list(yaml.safe_load_all(open('argocd/appsets/layer1-addons.yaml')))" && echo "OK"
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add argocd/appsets/layer1-addons.yaml
  git commit -m "fix: align layer1-addons versions with versions.yaml SSOT"
  ```

---

### Task 9: Create standalone Application for envoy-gateway-config

**Files:**
- Create: `argocd/bootstrap/envoy-gateway-config-app.yaml`

The layer1-addons ApplicationSet uses `source.chart` + `source.repoURL` for external Helm repos. But `envoy-gateway-config` is an in-repo chart that needs `source.path` instead — the matrix generator template can't handle both source types. Create a standalone Application manifest, applied by bootstrap.sh alongside the other bootstrap resources.

- [ ] **Step 1: Create `argocd/bootstrap/envoy-gateway-config-app.yaml`**

  ```yaml
  # Standalone Application for the in-repo envoy-gateway-config chart.
  # Cannot use the layer1-addons ApplicationSet because it templates
  # source.chart (external repos), while this needs source.path (git repo).
  apiVersion: argoproj.io/v1alpha1
  kind: Application
  metadata:
    name: envoy-gateway-config
    namespace: argocd
    annotations:
      argocd.argoproj.io/sync-wave: "2"
  spec:
    project: infra
    source:
      repoURL: https://github.com/catinspace-au/dfe-infra.git
      targetRevision: main
      path: helm/charts/envoy-gateway-config
      helm:
        valueFiles:
          - ../../../argocd/values/common.yaml
          - ../../../argocd/values/local.yaml
    destination:
      server: https://kubernetes.default.svc
      namespace: envoy-gateway-system
    syncPolicy:
      automated:
        prune: true
        selfHeal: true
      syncOptions:
        - CreateNamespace=true
        - ServerSideApply=true
  ```

  Note: `repoURL` and `targetRevision` are hardcoded here for the initial deployment. In a future iteration, this can be templated via a cluster-generator ApplicationSet for in-repo charts.

- [ ] **Step 2: Add to bootstrap.sh**

  In `bootstrap/bootstrap.sh`, add after the existing AppProject apply (step [7/7]):

  ```bash
  run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/envoy-gateway-config-app.yaml"
  ```

- [ ] **Step 3: Validate YAML**

  ```bash
  python3 -c "import yaml; yaml.safe_load(open('argocd/bootstrap/envoy-gateway-config-app.yaml'))" && echo "OK"
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add argocd/bootstrap/envoy-gateway-config-app.yaml bootstrap/bootstrap.sh
  git commit -m "feat: add standalone ArgoCD Application for envoy-gateway-config (in-repo chart)"
  ```

---

### Task 10: Smoke Test Script

**Files:**
- Create: `bootstrap/smoke-test.sh`

A quick verification script that checks all Layer 1 components are healthy after bootstrap.

- [ ] **Step 1: Create `bootstrap/smoke-test.sh`**

  ```bash
  #!/usr/bin/env bash
  #  Project:      dfe-infra
  #  File:         smoke-test.sh
  #  Purpose:      Verify Layer 1 components are healthy after bootstrap
  #  Language:     Bash
  #
  #  License:      FSL-1.1-ALv2
  #  Copyright:    (c) 2026 HYPERI PTY LIMITED
  set -euo pipefail

  readonly SCRIPT_NAME="$(basename "${0}")"
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

  echo "=== DFE Layer 1 Smoke Test ==="
  echo ""

  echo "--- Namespaces ---"
  check "argocd namespace exists" "kubectl get ns argocd"
  check "cert-manager namespace" "kubectl get ns cert-manager"
  check "external-secrets namespace" "kubectl get ns external-secrets"
  check "envoy-gateway-system namespace" "kubectl get ns envoy-gateway-system"

  echo ""
  echo "--- Core Pods ---"
  check "ArgoCD server running" "kubectl -n argocd get deploy argocd-server -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
  check "ArgoCD repo-server running" "kubectl -n argocd get deploy argocd-repo-server -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
  check "Valkey running" "kubectl -n argocd get statefulset dfe-valkey-master -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
  check "cert-manager running" "kubectl -n cert-manager get deploy cert-manager -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
  check "ESO running" "kubectl -n external-secrets get deploy external-secrets -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
  check "Envoy Gateway running" "kubectl -n envoy-gateway-system get deploy envoy-gateway -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"

  echo ""
  echo "--- ArgoCD State ---"
  check "Cluster secret exists" "kubectl -n argocd get secret dfe-cluster"
  check "AppProjects exist" "kubectl -n argocd get appproject infra data dfe-apps"
  check "Root ApplicationSet exists" "kubectl -n argocd get applicationset dfe-cluster-addons"

  echo ""
  echo "--- ESO ---"
  check "ClusterSecretStore healthy" "kubectl get clustersecretstore dfe-secret-store -o jsonpath='{.status.conditions[0].status}' | grep -q True"

  echo ""
  echo "=== Results: ${PASS} passed, ${FAIL} failed ==="

  if (( FAIL > 0 )); then
      echo "Layer 1 bootstrap is NOT healthy."
      exit 1
  else
      echo "Layer 1 bootstrap is healthy."
  fi
  ```

- [ ] **Step 2: Make executable, validate syntax**

  ```bash
  chmod +x bootstrap/smoke-test.sh
  bash -n bootstrap/smoke-test.sh && echo "OK"
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add bootstrap/smoke-test.sh
  git commit -m "feat: add Layer 1 smoke test script"
  ```

---

## Completion Criteria

This plan is complete when:
- [ ] `versions.yaml` exists at repo root with all version pins
- [ ] `python3 bootstrap/read_versions.py bootstrap.cert-manager` → `v1.17.2`
- [ ] `bootstrap/bootstrap.sh` reads versions dynamically from versions.yaml (no hardcoded version strings)
- [ ] `terraform validate` passes for all 3 new modules (tf-secrets, tf-iam, tf-storage)
- [ ] `terraform validate` passes for `terraform/environments/local/`
- [ ] `python3 -c "ast.parse(...)"` passes on `bootstrap/bridge.py`
- [ ] `helm lint helm/charts/envoy-gateway-config/` passes
- [ ] `bash -n bootstrap/bootstrap.sh` passes
- [ ] `bash -n bootstrap/smoke-test.sh` passes
- [ ] All versions in layer1-addons.yaml match versions.yaml (cross-checked via read_versions.py)
- [ ] All files committed and pushed to main

**Live deployment** (optional, after merge):
- [ ] `cd terraform/environments/local && terraform apply` provisions OpenBao secrets
- [ ] `python3 bootstrap/bridge.py --tf-dir terraform/environments/local` runs bootstrap.sh successfully
- [ ] `bash bootstrap/smoke-test.sh` → all checks pass

**Next plan:** `2026-03-30-dfe-infra-03-layer2-data.md` — Strimzi Kafka, CNPG PG17, ClickHouse, FerretDB, HyperDX, OTel Collector ApplicationSets deploying and healthy.
