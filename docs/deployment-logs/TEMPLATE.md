# Deployment Log: {environment} — {date}

## Environment
- **Target:** {cluster description}
- **Cloud:** local | aws | gcp | az
- **Domain:** {domain}
- **versions.yaml:** {git sha}

## Terraform
- **Command:** `deploy.sh --cloud {cloud}`
- **Resources created:** {count}
- **Issues:** {none | describe}

## Bootstrap
- **Duration:** {time}
- **Issues:** {none | describe}

## ArgoCD Sync
| Wave | Components | Status | Notes |
|------|-----------|--------|-------|
| 2 | cert-manager, ESO, Envoy Gateway | | |
| 3 | KEDA, operators | | |
| 4 | CNPG, Kafka, ClickHouse, FerretDB, OTel | | |
| 5 | DFE services, HyperDX | | |

## Smoke Tests
| Test | Result | Notes |
|------|--------|-------|
| Layer 1 | | |
| Data platform | | |
| Auth & ingress | | |
| KEDA | | |

## Issues Found + Fixes
1. {issue} → {fix} ({commit sha})

## Images Pending
- [ ] {service}: image not yet built
