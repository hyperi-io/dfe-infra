---
sidebar_position: 3
sidebar_label: Kubernetes on-premises
---

# Quickstart: Kubernetes on-premises

DFE on a Kubernetes cluster you run. RKE2 is the reference distribution, and the charts need no particular one.

You need Kubernetes 1.35 or later, `kubectl` pointed at the cluster, and `helm`, `openssl` and Python 3. `<version>` is the DFE release you are installing, the one in the version menu at the top of this page.

```bash
git clone --branch <version> https://github.com/hyperi-io/dfe-infra && cd dfe-infra
(umask 077 && cp bootstrap/local.env.example bootstrap/local.env)
python3 scripts/dfe-ops preflight --mode single --env-file bootstrap/local.env
python3 scripts/dfe-ops stack-deploy --stack <version> --mode single \
  --target-revision <version> --env-file bootstrap/local.env
```

Before the preflight, set two things in `bootstrap/local.env`. `DFE_DOMAIN` is the domain every hostname sits under. `DFE_REPO_URL` is the git repository Argo CD reads the charts from, `https://github.com/hyperi-io/dfe-infra` or your own copy of it. The file stays readable by you only, and git ignores it.

The preflight is read-only. It checks the cluster is fit for the tier before anything changes. `stack-deploy` installs cert-manager, External Secrets and Argo CD, or adopts healthy ones already there. Argo CD then syncs the backing services and the DFE apps at the versions `<version>` pins. It finishes by sending an event through to ClickHouse and checking the stack's own telemetry landed, then writes your logins to a file only you can read.

`--mode` picks the tier:

| Tier | Transport | Shape |
|---|---|---|
| `slim` | gRPC | One replica of each stage, no broker. |
| `single` | Kafka | One replica of each, with a single Kafka broker in the cluster. |
| `scale` | Kafka | Replicas, a ClickHouse cluster and Strimzi Kafka. Reads its broker credential from a secrets store, OpenBao or AWS Secrets Manager. |

Next:

1. The layers, tiers and values cascade: [Deploying DFE with dfe-infra](../dfe-infra/deployment/index.md).
2. Prepare the cluster: [RKE2, the DFE Kubernetes distribution](../dfe-infra/deployment/rke2.md).
3. Decide where data is stored, before the first deploy: [storage](../dfe-infra/deployment/storage.md).
4. Deploy, test and tear down: [on-premises guide](../dfe-infra/deployment/on-prem.md).
5. Move to a newer release: [upgrades](../dfe-infra/deployment/upgrades.md).
