# Repository Directory Structure

This document is the canonical reference for what lives where in `dfe-infra`.
Every directory has one owner and one purpose.

```
dfe-infra/
├── terraform/
│   └── modules/              # Reusable Terraform/OpenTofu modules (one per resource type)
│       ├── tf-naming/        # Canonical naming standard — import into every other module
│       ├── tf-k8s-cluster/   # K8s cluster provisioning (RKE2 / EKS / GKE / AKS)
│       ├── tf-networking/    # VPC/VNet, subnets, NAT gateway
│       ├── tf-secrets/       # Secrets backend (OpenBao / AWS SM / GCP SM / Azure KV)
│       ├── tf-storage/       # Cloud storage buckets (config, archiving, backups)
│       ├── tf-iam/           # Workload identity per DFE service (IRSA/WIF/Azure WI/AppRole)
│       ├── tf-dns/           # DNS zone + records
│       └── tf-certs/         # TLS wildcard cert via cert-manager ACME DNS-01
│
├── helm/
│   ├── library/
│   │   └── dfe-common/       # Shared Helm helpers (labels, names) — type: library
│   └── charts/               # One chart per DFE service or data component
│       ├── dfe-engine/       # Python control plane
│       ├── dfe-ui/           # Next.js UI + HyperDX integration
│       ├── dfe-receiver/     # Rust ingest (listens on 100.64.x.x)
│       ├── dfe-loader/       # Rust → ClickHouse writer
│       ├── dfe-archiver/     # Rust → S3/MinIO archiver
│       ├── dfe-fetcher/      # Rust API fetcher
│       ├── cnpg-cluster/     # CNPG PostgreSQL 17 cluster CRD
│       ├── clickhouse-cluster/ # ClickHouse cluster CRD (Altinity operator)
│       ├── strimzi-kafka/    # Strimzi Kafka (KRaft, SASL/SCRAM)
│       ├── ferretdb/         # FerretDB (MongoDB wire protocol over CNPG PG17)
│       ├── hyperdx/          # HyperDX (observability UI)
│       └── otel-collector/   # OTel Collector (DaemonSet + Gateway tiers)
│
├── argocd/
│   ├── bootstrap/            # Applied once by bootstrap.sh before ArgoCD self-manages
│   │   ├── appproject-bootstrap.yaml
│   │   └── argocd-cluster-addons.yaml
│   ├── appsets/              # Layer ApplicationSets (matrix generator pattern)
│   │   ├── layer1-addons.yaml    # Wave 2-3: operators
│   │   ├── layer2-data.yaml      # Wave 4: data platform
│   │   └── layer2-apps.yaml      # Wave 5: DFE services
│   └── values/               # Helm value overrides per cloud target
│       ├── common.yaml       # Defaults (all clouds inherit)
│       ├── local.yaml        # Rancher local (RKE2)
│       ├── aws.yaml          # AWS EKS
│       ├── gcp.yaml          # GCP GKE
│       └── azure.yaml        # Azure AKS
│
├── bootstrap/
│   ├── bootstrap.sh          # Build 1: idempotent static bootstrap (run once per cluster)
│   └── templates/            # envsubst templates rendered by bootstrap.sh
│       ├── cluster-secret.yaml.tpl   # ArgoCD cluster secret (annotation bridge)
│       └── eso-cluster-secret-store.yaml.tpl
│
├── docs/                     # Research corpus + specs + plans
│   ├── 01-dfe-core-analysis.md through 07-synthesis.md
│   ├── license-review.md
│   └── superpowers/specs/ and superpowers/plans/
│
├── .github/workflows/        # CI pipelines
│   ├── tf-validate.yml       # Terraform/OpenTofu validate + test (matrix: both)
│   └── helm-lint.yml         # Helm lint + ArgoCD YAML validation
│
├── CLAUDE.md                 # Project context for AI agents
├── DIRECTORY.md              # ← this file
├── SCOPE.md                  # Project scope and constraints
└── TODO.md                   # Task tracking
```

## Conventions

| Convention | Rule |
|-----------|------|
| IaC tool | Terraform >=1.6 OR OpenTofu >=1.6 (both supported, common HCL subset) |
| Naming | All resource names from tf-naming module. See spec Section 9. |
| Cloud values | Cloud-specific Helm values in `argocd/values/{cloud}.yaml` only. |
| Secrets | Never commit secrets. All secrets via ESO. |
| Chart deps | All charts depend on `helm/library/dfe-common` for labels/names. |
| Testing | TF: `terraform/modules/{mod}/tests/`. Helm: `helm/charts/{chart}/templates/tests/`. |
| Bootstrap | Build 1 (bootstrap.sh) is static/release-pinned. Build 2 (ArgoCD) is dynamic/GitOps. |
| Observability | OTel → ClickHouse → HyperDX. No Prometheus, Grafana, or CloudWatch. |
