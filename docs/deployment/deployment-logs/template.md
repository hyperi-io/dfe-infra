# Deployment Log: {environment} -- {date}

## Environment

| Field | Value |
|---|---|
| Target | {cluster description} |
| Cloud | local, aws, gcp or azure |
| Domain | {domain} |
| versions.yaml | {git sha} |

## OpenTofu

| Field | Value |
|---|---|
| Command | `deploy.sh --cloud {cloud}` |
| Resources created | {count} |
| Issues | {none, or describe} |

## Bootstrap

| Field | Value |
|---|---|
| Duration | {time} |
| Issues | {none, or describe} |

## Argo CD sync

| Wave | Components | Status | Notes |
|------|-----------|--------|-------|
| bootstrap | cert-manager, ESO, Argo CD, AppProjects | | |
| 1-3 | Karpenter, network policies, Envoy Gateway config, KEDA, operators (DocumentDB and its CloudNativePG included) | | |
| 4 | Kafka, ClickHouse, DocumentDB, deploy-repo Forgejo | | |
| 5 | dfe-engine | | |
| 6 | OTel collector | | |
| 7 | DFE apps, dfe-hyperdx | | |

## Smoke Tests

| Test | Result | Notes |
|------|--------|-------|
| Layer 1 | | |
| Data platform | | |
| Auth & ingress | | |
| KEDA | | |

## Issues Found + Fixes

1. {issue} -> {fix} ({commit sha})

## Images Pending

- [ ] {service}: image missing from the registry
