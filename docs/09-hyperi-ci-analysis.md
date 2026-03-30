# Hyperi-CI Analysis for dfe-infra

**Date:** 2026-03-30
**Scope:** CI/CD strategy for dfe-infra — both self-validation and DFE deployment pipelines
**Status:** Research complete

---

## Table of Contents

1. [Hyperi-CI Architecture](#1-hyperi-ci-architecture)
2. [Semantic Release Pattern](#2-semantic-release-pattern)
3. [Two-CI Model for dfe-infra](#3-two-ci-model-for-dfe-infra)
4. [What to Reuse from Hyperi-CI](#4-what-to-reuse-from-hyperi-ci)
5. [Integration Plan](#5-integration-plan)

---

## 1. Hyperi-CI Architecture

### 1.1 What Hyperi-CI Is

Hyperi-CI (`hyperi-ci`) is an in-house polyglot CI/CD CLI tool developed by HyperI. It provides a unified interface for running quality checks, tests, builds, and publishing across projects written in Rust, Python, TypeScript, Go, C++, and Bash. The same tool runs identically on developer machines and in GitHub Actions, eliminating the "works locally but fails in CI" class of problems.

The tool is distributed as a Python package and installed via `uv tool install hyperi-ci` or `pip install --user hyperi-ci`. It is not a library to be imported -- it is a command-line tool.

**Repository:** `github.com/hyperi-io/hyperi-ci` (private, not present on this machine)

### 1.2 How Projects Consume It

Consumer projects interact with hyperi-ci through three layers:

**Layer 1: Configuration file (`.hyperi-ci.yaml`)**

The project root contains a `.hyperi-ci.yaml` that declares the project's language, build strategy, quality tools, and publishing configuration:

```yaml
language: rust          # auto-detected if omitted
publish:
  enabled: true
  target: both          # internal | oss | both
  channel: release      # spike | alpha | beta | release
build:
  strategies: [native]
  rust:
    targets:
      - x86_64-unknown-linux-gnu
      - aarch64-unknown-linux-gnu
quality:
  enabled: true
  gitleaks: blocking
```

Configuration cascades: CLI flags > `HYPERCI_*` env vars > `.hyperi-ci.yaml` > `config/defaults.yaml` > hardcoded defaults.

**Layer 2: Thin GitHub Actions workflow (`.github/workflows/ci.yml`)**

Consumer projects have a minimal workflow file that delegates to reusable workflows hosted in the `hyperi-io/hyperi-ci` repository:

```yaml
name: CI
on:
  push: { branches: ["**"] }
  pull_request: { branches: [main] }
  workflow_dispatch:
jobs:
  ci:
    uses: hyperi-io/hyperi-ci/.github/workflows/rust-ci.yml@main
    with: { publish-target: both }
    secrets: inherit
```

The reusable workflow handles the full pipeline: quality, test, build, release (semantic-release on main), and publish (on `workflow_dispatch`). Language-specific workflow files exist (e.g. `rust-ci.yml`, and presumably `python-ci.yml`, `typescript-ci.yml`).

**Layer 3: Generated Makefile + git hooks**

Running `hyperi-ci init` scaffolds:
- `.hyperi-ci.yaml` -- project configuration
- `Makefile` -- standard targets (`check`, `quality`, `test`, `build`, `ci`)
- `.github/workflows/ci.yml` -- the thin workflow calling the reusable one
- `.releaserc.yaml` -- semantic-release configuration
- `.githooks/commit-msg` -- conventional commit validation hook

### 1.3 CLI Commands

| Command | Purpose |
|---------|---------|
| `hyperi-ci check` | Pre-push validation (quality + test) -- mandatory before every push |
| `hyperi-ci check --quick` | Quality only (lint, format, type check) |
| `hyperi-ci check --full` | Quality + test + build (native target) |
| `hyperi-ci run quality` | Lint, format, type check, security audit |
| `hyperi-ci run test` | Run test suite with coverage |
| `hyperi-ci run build` | Build artifacts |
| `hyperi-ci run publish` | Publish (CI only) |
| `hyperi-ci detect` | Show detected language |
| `hyperi-ci config` | Show merged config as JSON |
| `hyperi-ci trigger` | Trigger GitHub Actions workflow |
| `hyperi-ci watch` | Watch latest CI run |
| `hyperi-ci logs --failed` | Show only failed job logs |
| `hyperi-ci release --list` | Show unpublished version tags |
| `hyperi-ci release v1.3.0` | Trigger publish workflow for a tag |
| `hyperi-ci release-merge` | Merge main into release branch via PR |
| `hyperi-ci check-commit --list` | Show all accepted commit type prefixes |
| `hyperi-ci init` | Scaffold a new project |
| `hyperi-ci migrate` | Convert legacy `ci/` submodule to hyperi-ci |

### 1.4 Pipeline Stages

Quality checks are language-aware and configured via `.hyperi-ci.yaml`:

| Language | Quality | Test | Build |
|----------|---------|------|-------|
| **Rust** | `cargo fmt --check`, `cargo clippy`, `cargo audit` | `cargo nextest run` or `cargo test` | `cargo build` |
| **Python** | `ruff check`, `ruff format --check`, ty/pyright, `pip-audit`, `bandit` | `pytest` with coverage | wheel + container |
| **TypeScript** | `eslint`, `prettier --check`, `tsc --noEmit` | `vitest` or `jest` | Next.js/Turbopack |
| **Go** | `golangci-lint`, `go vet`, `govulncheck` | `go test ./...` | `go build` |

### 1.5 Pipeline Flow

**On push to main:**
```
quality --> test --> build (amd64 validation only) --> semantic-release (tag + version commit)
```

**On workflow_dispatch (publish):**
```
checkout tag --> quality --> test --> build (amd64 + arm64 cross-compile) --> publish
```

The publish pipeline is a complete standalone run from the tagged commit. No artifacts are shared from the main push -- the tag checkout guarantees the source has the correct version baked in.

### 1.6 Legacy CI System

Before hyperi-ci, HyperI projects used a `ci/` git submodule pointing to `hyperi-io/ci`. This is now deprecated. `hyperi-ci migrate` automates conversion. The old submodule-based system should not be used for new projects.

### 1.7 Authentication in CI

GitHub App tokens are preferred over PATs for CI push-back and cross-repo access:

```yaml
- uses: actions/create-github-app-token@v2
  with:
    app-id: ${{ secrets.GH_APP_ID }}
    private-key: ${{ secrets.GH_APP_PRIVATE_KEY }}
```

Other conventions:
- Pin actions to full SHA, not tags
- Use `vars.*` for non-secret config, `secrets.*` for credentials
- Default runner: `${{ vars.GH_RUNNER_DEFAULT || 'ubuntu-latest' }}`

---

## 2. Semantic Release Pattern

### 2.1 Single Versioning on Main

Hyperi-CI uses a single-branch versioning model. There is no release branch for versioning purposes. All versions are determined on `main` by semantic-release:

```
Developer pushes to main
  --> CI runs quality, test, build
  --> semantic-release determines next version (e.g. 1.3.0)
  --> Updates VERSION + language manifest, commits, creates git tag v1.3.0
  --> Tag exists on main. Nothing published yet.

Developer decides to ship:
  --> hyperi-ci release v1.3.0
  --> Dispatches workflow: quality -> test -> build (full) -> publish
  --> Creates GH Release, uploads binaries to R2, publishes to registries
```

Key properties:
- `VERSION` file in repo root is the source of truth -- written by semantic-release
- `CHANGELOG.md` is auto-generated from conventional commits
- Skipped versions are normal (not every tag needs to be published)
- After a push, semantic-release creates a `chore(release): X.Y.Z [skip ci]` commit that pushes the local branch one commit behind -- always `git pull --rebase origin main` before next push

### 2.2 Semantic Release Configuration

Generated by `hyperi-ci init` as `.releaserc.yaml`:
- `branches: [main]` -- runs only on main
- No `@semantic-release/github` plugin -- GH Release is created by the publish step
- `prepareCmd` updates VERSION and the language manifest
- `@semantic-release/git` commits changes with `[skip ci]` to prevent loops
- `@semantic-release/changelog` generates CHANGELOG.md

### 2.3 Commit Message Convention

Enforced by both `.githooks/commit-msg` (local) and the CI quality stage (remote):

```
<type>: <description>
<type>(scope): <description>
```

**Version bump types:**
- `feat:` -- MINOR (new user-facing feature, use sparingly)
- `fix:` -- PATCH (default choice for most changes)
- `perf:`, `hotfix:`, `security:`/`sec:` -- PATCH

**No bump types (maintenance):**
- `docs`, `test`, `refactor`, `style`, `build`, `ci`, `chore`, `deps`, `revert`, `wip`, `cleanup`, `data`, `debt`, `design`, `infra`, `meta`, `ops`, `review`, `spike`, `ui`

**BREAKING CHANGE:** `BREAKING CHANGE:` in commit body triggers MAJOR bump. Never automated -- requires explicit human decision.

### 2.4 Publishing Channels

Set in `.hyperi-ci.yaml` under `publish.channel`:

| Channel | GH Release | R2 Path | Registry Publishing |
|---------|------------|---------|---------------------|
| `spike` | Prerelease | `/{project}/spike/v1.3.0/` | Skipped |
| `alpha` | Prerelease | `/{project}/alpha/v1.3.0/` | Skipped |
| `beta` | Prerelease | `/{project}/beta/v1.3.0/` | Skipped |
| `release` | GA | `/{project}/v1.3.0/` | Published (PyPI, crates.io, npm) |

Projects graduate through channels by changing one line in `.hyperi-ci.yaml`.

### 2.5 Versioning for Infrastructure Repos

The hyperi-ci model is designed for single-language application repos. An infrastructure repo like dfe-infra presents a challenge because it contains:
- Terraform modules (HCL)
- Helm charts (YAML)
- ArgoCD manifests (YAML)
- Bootstrap scripts (Bash)
- Docker build contexts

This is a **multi-artifact repo**. Semantic-release's single `VERSION` file model works best when the repo produces one logical release artifact. For dfe-infra, the version represents "the state of the complete deployment specification" rather than a single binary or package. This is a valid and common pattern for IaC repos -- the version tags the entire deployment definition, and consumers (ArgoCD) reference a specific `targetRevision` (tag or branch).

---

## 3. Two-CI Model for dfe-infra

### 3.1 Overview

dfe-infra needs two distinct CI purposes:

| Pipeline | Purpose | Trigger | Output |
|----------|---------|---------|--------|
| **Self CI** | Validate dfe-infra's own code | Push/PR to dfe-infra repo | Green/red validation status |
| **Deployment CI** | Deploy DFE clusters using dfe-infra | Workflow dispatch or tag-based release | Running DFE cluster |

These are fundamentally different in nature. Self CI is defensive (catch errors before they reach main). Deployment CI is constructive (build real infrastructure).

### 3.2 Self CI -- Validate dfe-infra's Own Code

This pipeline validates the repository's own artefacts on every push and PR. dfe-infra already has three workflow files that partially cover this:

**Existing workflows:**

| Workflow | File | What It Validates |
|----------|------|-------------------|
| Terraform Validate | `.github/workflows/tf-validate.yml` | `tofu init + validate` on all modules; `tofu test` on modules with `.tftest.hcl` files. Runs both `terraform` and `tofu` (matrix strategy). |
| Helm Lint | `.github/workflows/helm-lint.yml` | `helm lint` on library chart test charts and application charts. YAML syntax validation on all ArgoCD manifests. |
| Docker Build | `.github/workflows/docker-build.yml` | Builds and pushes utility container images to JFrog on merge to main. |

**Gaps to fill:**

| Missing Check | Tool | Priority |
|---------------|------|----------|
| Bootstrap script validation | `bash -n`, `shellcheck`, BATS tests | High -- bootstrap.sh is critical path |
| Conventional commit enforcement | `commitlint` or `hyperi-ci check-commit` | High -- required for semantic-release |
| YAML linting beyond syntax | `yamllint` (checks indentation, line length, trailing spaces) | Medium |
| Security scanning | `gitleaks` (secrets in code), `trivy` (container images) | High |
| Kubernetes manifest validation | `kubeconform` or `kubectl --dry-run=server` | Medium |
| Terraform fmt check | `tofu fmt -check -recursive` | Medium |

**Recommended consolidated Self CI workflow:**

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  terraform:
    # Existing tf-validate.yml logic (matrix: terraform + tofu)
    # Add: tofu fmt -check -recursive

  helm:
    # Existing helm-lint.yml logic
    # Add: kubeconform on rendered templates

  bootstrap:
    # bash -n bootstrap/bootstrap.sh
    # shellcheck bootstrap/bootstrap.sh
    # bats bootstrap/tests/bootstrap.bats (dry-run tests)

  security:
    # gitleaks detect --source . --verbose
    # trivy fs --scanners vuln,misconfig .

  commits:
    # Validate conventional commit messages in the PR range
    # commitlint or hyperi-ci check-commit equivalent
```

### 3.3 Deployment CI -- Deploy DFE Using dfe-infra

This is the pipeline that takes the validated dfe-infra code and uses it to deploy or update a DFE cluster. It is triggered manually (workflow_dispatch) or by release tags, and operates on real infrastructure.

**Pipeline stages:**

```
1. Checkout tagged/released version of dfe-infra
2. Terraform plan (against target cloud state)
3. Manual approval gate (for production)
4. Terraform apply (Layer 1: K8s cluster + base infra)
5. Run bootstrap.sh (cert-manager, ESO, ArgoCD, Valkey)
6. ArgoCD takes over (Layer 2: data platform + DFE apps)
7. Health checks (wait for all ArgoCD Applications to sync and be healthy)
8. Smoke test (verify OTel flows, HyperDX accessible, dfe-engine API responds)
```

This pipeline is fundamentally different from Self CI:
- It needs cloud credentials (AWS/GCP/Azure or kubeconfig for Rancher local)
- It runs `terraform apply` against real state
- It has approval gates for production environments
- It is environment-aware (dev/stg/prod/local)

**Recommended deployment workflow structure:**

```yaml
name: Deploy DFE
on:
  workflow_dispatch:
    inputs:
      environment:
        type: choice
        options: [local, dev, stg, prod]
      cloud:
        type: choice
        options: [local, aws, gcp, az]
      action:
        type: choice
        options: [plan, apply, destroy]

jobs:
  terraform:
    environment: ${{ inputs.environment }}
    # terraform plan/apply for Layer 1

  bootstrap:
    needs: [terraform]
    if: inputs.action == 'apply'
    # Run bootstrap.sh with outputs from terraform

  verify:
    needs: [bootstrap]
    # Wait for ArgoCD sync, run health checks
```

### 3.4 Semantic Release for dfe-infra

For an infrastructure repo, semantic-release versions the complete deployment specification. Each version tag represents a tested, validated, deployable state of the entire dfe-infra codebase.

**How it works:**

1. Developers push changes to `main` with conventional commits
2. Self CI validates (terraform validate, helm lint, shellcheck, BATS)
3. Semantic-release creates a version tag (e.g. `v2.2.0`)
4. The deployment pipeline can be triggered against any tag
5. ArgoCD `targetRevision` in the cluster secret references the tag

**Version semantics for dfe-infra:**

| Bump | Meaning |
|------|---------|
| MAJOR | Breaking change to cluster secret annotations, bootstrap.sh interface, or Terraform module inputs that requires re-bootstrapping existing clusters |
| MINOR | New Terraform module, new Helm chart, new deployment target (e.g. adding GCP support), new ArgoCD ApplicationSet |
| PATCH | Bug fix in existing module/chart, version bump of upstream dependency, config correction |

**What semantic-release updates:**
- `VERSION` file
- `CHANGELOG.md`
- Git tag

**What semantic-release does NOT update** (unlike application repos):
- No `Cargo.toml`, `pyproject.toml`, `package.json` -- dfe-infra is not a compiled artifact
- No container image tags -- those are managed by the docker-build workflow using git SHA
- No Helm chart versions -- those are managed independently per chart

### 3.5 Relationship Between the Two Pipelines

```
Self CI (automated, every push)
  |
  | validates code quality
  | semantic-release creates version tag
  |
  v
Deployment CI (manual trigger, per environment)
  |
  | uses a specific version tag
  | terraform apply + bootstrap + ArgoCD sync
  |
  v
Running DFE cluster
```

The Self CI pipeline gates the Deployment CI pipeline. No deployment should use a version that has not passed Self CI.

---

## 4. What to Reuse from Hyperi-CI

### 4.1 Patterns to Adopt Directly

| Pattern | Source | How to Adopt |
|---------|--------|-------------|
| Conventional commit enforcement | `.githooks/commit-msg` from `hyperi-ci init` | Run `hyperi-ci init` or manually create the hook. Validate in CI with `commitlint`. |
| Semantic-release on main | `.releaserc.yaml` | Generate with `hyperi-ci init` or create manually. `branches: [main]`, standard plugin chain. |
| Pre-push local validation | `hyperi-ci check` / `make check` | Create a `Makefile` with `check`, `quality`, `test` targets that mirror CI stages. |
| GitHub App tokens for CI push-back | `actions/create-github-app-token@v2` | Semantic-release needs push permission to commit VERSION and CHANGELOG back to main. |
| Publishing channels | `.hyperi-ci.yaml` `publish.channel` | Start dfe-infra at `alpha`, graduate to `release` when stable. |

### 4.2 Patterns That Need Adaptation

| Pattern | Why It Needs Adaptation |
|---------|------------------------|
| Reusable workflow (`hyperi-ci/.github/workflows/rust-ci.yml@main`) | dfe-infra is not a single-language project. Cannot use the language-specific reusable workflows. Must write custom workflows. |
| `hyperi-ci run quality` | hyperi-ci's quality stage is language-specific (ruff for Python, clippy for Rust). dfe-infra needs infra-specific quality: `tofu validate`, `helm lint`, `shellcheck`, `yamllint`, `gitleaks`. |
| `hyperi-ci run test` | hyperi-ci runs `pytest`, `cargo test`, etc. dfe-infra tests are `tofu test`, `bats`, `helm template --dry-run`. Custom test targets needed. |
| Single `VERSION` file | Works well for dfe-infra's use case (versioning the deployment spec). Adopt as-is. |

### 4.3 Patterns NOT to Adopt

| Pattern | Why Not |
|---------|---------|
| Language-specific reusable workflows | dfe-infra is multi-tool (Terraform + Helm + Bash), not a single-language application repo. |
| `hyperi-ci run build` | dfe-infra does not produce a compiled binary. The "build" is `terraform plan` + `helm template`, which is part of the deployment pipeline, not the self-CI. |
| `hyperi-ci release-merge` (main to release branch) | The release-merge flow is designed for application repos that publish binaries. dfe-infra releases are consumed by ArgoCD via git tag reference, not by merging to a release branch. |
| Build matrix (amd64 + arm64 cross-compile) | Not applicable to IaC. The docker-build workflow already handles multi-arch for utility images. |

### 4.4 Key Files from Hyperi-CI Standards to Reference

| File | Location in dfe-infra | Key Content |
|------|----------------------|-------------|
| CI standards (full) | `hyperi-ai/standards/infrastructure/CI.md` | Complete CI/CD architecture, versioning, commit format, publishing |
| CI rules (compact) | `hyperi-ai/standards/rules/ci.md` | Quick reference for CI rules |
| Universal CI | `hyperi-ai/standards/universal/CI.md` | Cross-cutting CI standards, authentication, type checking |
| Release skill | `hyperi-ai/skills/release/SKILL.md` | Full release workflow steps |
| CI check skill | `hyperi-ai/skills/ci-check/SKILL.md` | Local pre-push validation procedure |
| CI watch skill | `hyperi-ai/skills/ci-watch/SKILL.md` | Monitoring CI runs |
| CI logs skill | `hyperi-ai/skills/ci-logs/SKILL.md` | Debugging CI failures |

---

## 5. Integration Plan

### 5.1 Phase 1: Adopt Semantic Release (Immediate)

**Goal:** Enable versioned releases of dfe-infra.

1. Create `.releaserc.yaml` on main:
   ```yaml
   branches: [main]
   plugins:
     - "@semantic-release/commit-analyzer"
     - "@semantic-release/release-notes-generator"
     - "@semantic-release/changelog"
     - ["@semantic-release/exec", {
         "prepareCmd": "echo ${nextRelease.version} > VERSION"
       }]
     - ["@semantic-release/git", {
         "assets": ["VERSION", "CHANGELOG.md"],
         "message": "chore(release): ${nextRelease.version} [skip ci]"
       }]
   ```

2. Create `VERSION` file (initial: `0.1.0`)

3. Create `.githooks/commit-msg` (from hyperi-ci convention) and activate:
   ```bash
   git config core.hooksPath .githooks
   ```

4. Add semantic-release step to the Self CI workflow, running only on main:
   ```yaml
   release:
     needs: [terraform, helm, bootstrap, security]
     if: github.ref == 'refs/heads/main'
     runs-on: ubuntu-latest
     steps:
       - uses: actions/checkout@v4
         with:
           token: ${{ steps.app-token.outputs.token }}
           fetch-depth: 0
       - uses: actions/setup-node@v4
       - run: npx semantic-release
   ```

### 5.2 Phase 2: Consolidate Self CI (Short Term)

**Goal:** Merge the three existing workflows into a unified CI pipeline with additional checks.

Keep the existing `tf-validate.yml`, `helm-lint.yml`, and `docker-build.yml` as separate workflow files (GitHub Actions best practice: separate workflows for separate concerns, parallel execution, independent failure). Add these new workflows:

1. **`.github/workflows/bootstrap-validate.yml`** -- shellcheck + BATS tests for bootstrap.sh
2. **`.github/workflows/security.yml`** -- gitleaks + trivy
3. **`.github/workflows/release.yml`** -- semantic-release on main (separate from validation)

Create a `Makefile` at the repo root for local development:
```makefile
.PHONY: check quality test

check: quality test    ## Pre-push validation (mirrors CI)

quality:               ## Lint, format check, security scan
    tofu fmt -check -recursive terraform/
    shellcheck bootstrap/bootstrap.sh
    yamllint argocd/ bootstrap/templates/
    gitleaks detect --source . --verbose --no-banner

test:                  ## Run all tests
    cd terraform/modules/tf-naming && tofu test
    helm lint helm/library/dfe-common/tests/lint-test/
    bash -n bootstrap/bootstrap.sh
    bats bootstrap/tests/bootstrap.bats
```

### 5.3 Phase 3: Create `.hyperi-ci.yaml` (When Supported)

**Goal:** Bring dfe-infra under the hyperi-ci umbrella when the tool supports multi-tool/infra projects.

Currently, hyperi-ci's `language:` field is designed for single-language projects (rust, python, typescript, go). An infrastructure repo does not fit this model.

**Two possible paths:**

**Option A (Preferred): hyperi-ci gains infra support.** If hyperi-ci adds a `language: infra` or `language: multi` mode that runs tofu validate, helm lint, shellcheck, and BATS -- adopt it. The `.hyperi-ci.yaml` would look like:

```yaml
language: infra
quality:
  enabled: true
  terraform: blocking
  helm: blocking
  shellcheck: blocking
  yamllint: blocking
  gitleaks: blocking
publish:
  enabled: false   # dfe-infra publishes via ArgoCD, not registries
```

**Option B (Current reality): Custom workflows.** Continue with the custom GitHub Actions workflows from Phase 2. The hyperi-ci CLI can still be used for:
- `hyperi-ci check-commit` (validating commit messages)
- `hyperi-ci release --list` (listing unpublished tags)
- `hyperi-ci release v2.2.0` (triggering publish/deployment workflow)
- `hyperi-ci watch` / `hyperi-ci logs --failed` (monitoring CI runs)

### 5.4 Phase 4: Deployment Pipeline (Medium Term)

**Goal:** Create the deployment CI that uses dfe-infra to deploy DFE clusters.

This is a separate workflow file (`.github/workflows/deploy.yml`) that:
- Is triggered by `workflow_dispatch` with environment/cloud/action inputs
- Uses GitHub Environments for approval gates and cloud credentials
- Runs `terraform plan` and `terraform apply`
- Runs `bootstrap.sh`
- Waits for ArgoCD health
- Runs smoke tests

This workflow does NOT use hyperi-ci's reusable workflows. It is custom to dfe-infra's deployment model.

### 5.5 Summary: What Goes Where

| Concern | Implementation | Uses hyperi-ci? |
|---------|---------------|-----------------|
| Commit message validation | `.githooks/commit-msg` + CI check | Yes (convention from hyperi-ci) |
| Semantic versioning | `.releaserc.yaml` + semantic-release in CI | Yes (convention from hyperi-ci) |
| Terraform validation | `.github/workflows/tf-validate.yml` | No (custom) |
| Helm linting | `.github/workflows/helm-lint.yml` | No (custom) |
| Bootstrap validation | `.github/workflows/bootstrap-validate.yml` | No (custom) |
| Security scanning | `.github/workflows/security.yml` | No (custom, uses gitleaks/trivy) |
| Container image build | `.github/workflows/docker-build.yml` | No (custom) |
| Release tagging | `.github/workflows/release.yml` | Convention from hyperi-ci |
| DFE deployment | `.github/workflows/deploy.yml` | No (custom) |
| Local pre-push check | `make check` | Follows hyperi-ci pattern |
| Monitoring CI runs | `hyperi-ci watch`, `gh run watch` | Yes (CLI tool) |
| Debugging CI failures | `hyperi-ci logs --failed`, `gh run view --log-failed` | Yes (CLI tool) |

### 5.6 Channel Strategy for dfe-infra

Start with:
```yaml
publish:
  channel: alpha
```

Graduate to `beta` after successful Rancher local deployment. Graduate to `release` after at least one cloud target (AWS EKS) is proven.

This maps to ArgoCD `targetRevision`:
- `alpha` channel: teams use `main` branch as targetRevision (bleeding edge)
- `beta` channel: teams use latest beta tag
- `release` channel: production deployments use GA version tags (e.g. `v2.2.0`)

---

## Key Findings

1. **hyperi-ci is designed for single-language application repos.** dfe-infra cannot use its reusable workflows directly but can adopt its conventions (semantic-release, conventional commits, pre-push checks, publishing channels).

2. **dfe-infra already has a solid Self CI foundation.** The three existing workflows (`tf-validate.yml`, `helm-lint.yml`, `docker-build.yml`) cover the core validation. Gaps are bootstrap validation, security scanning, and semantic-release.

3. **The two-CI model is essential.** Self CI (validate code) and Deployment CI (deploy infrastructure) have fundamentally different triggers, credentials, and failure modes. They must be separate workflows.

4. **Semantic-release works well for IaC repos.** The single `VERSION` file versioning the entire deployment specification is a clean model. ArgoCD `targetRevision` consumes these tags directly.

5. **The hyperi-ci CLI is useful even without `.hyperi-ci.yaml`.** Commands like `watch`, `logs --failed`, `check-commit`, and `release` work for any GitHub-hosted project. Install with `uv tool install hyperi-ci` for developer ergonomics.
