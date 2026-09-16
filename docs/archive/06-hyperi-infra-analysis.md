# Hyperi-Infra Repository Analysis for DFE 2.2 Local Rancher Deployment

**Date:** 2026-03-30
**Repository:** `hyperi-io/hyperi-infra`
**Purpose:** Inform DFE 2.2 local Rancher deployment target

---

## 1. Architecture Overview

### 1.1 Overall Infrastructure Design

The `hyperi-infra` repository implements a complete IaC environment running on a single bare-metal hypervisor host (`example.com`). Designed as a single-node DevEx environment with HA-ready code paths.

**Three-tier complexity hierarchy:**

```
Tier 2 (Complex)   : Rancher, K8s workloads, ARC runners, data services
                       depends on
Tier 1 (Moderate)  : OpenBao, Harbor (Docker Compose on VMs)
                       depends on
Tier 0 (Simple)    : CoreDNS, Kea DHCP, PostgreSQL, chrony (systemd/Docker on infra VM)
```

**Key principle:** Services at lower tiers MUST be simpler than higher tiers. Tier 0 services fixable with `vim` and `systemctl`. Infrastructure runs OUTSIDE K8s because K8s depends on DNS, registry, and secrets.

### 1.2 What Rancher Manages

Rancher v2.11.1 deployed as Helm chart into `cattle-system` namespace on the local RKE2 cluster:
- `ingress.enabled=false` (uses Envoy Gateway HTTPRoute instead)
- `tls=external` (TLS at Envoy Gateway)
- Manages the LOCAL RKE2 cluster only
- **NOT used to provision/manage cloud clusters** (EKS/GKE/AKS use native tools)

### 1.3 K8s Cluster Topology

3-node HA RKE2 cluster with embedded etcd, all nodes converged (control-plane + worker):

| Node | IP | CPU | RAM | Data Disks |
|------|-----|-----|-----|------------|
| k8s-1 | 192.0.2.201 | 24 cores | 64GB | 500GB HDD + 200GB NVMe |
| k8s-2 | 192.0.2.202 | 24 cores | 64GB | 500GB HDD + 200GB NVMe |
| k8s-3 | 192.0.2.203 | 24 cores | 64GB | 500GB HDD + 200GB NVMe |

- **API VIP:** 192.0.2.200 (Keepalived)
- **CNI:** Canal (Flannel + Calico)
- **RKE2:** v1.32.3+rke2r1
- **nginx-ingress disabled** -- Envoy Gateway instead

### 1.4 Network Design

Single flat network on the hypervisor bridge `vmbr1`:
- 192.0.2.100-199: Pet VMs (static IPs)
- 192.0.2.200-250: VIPs and K8s externalIPs
- Wildcard DNS: `*.apps.example.com` -> K8s VIP (192.0.2.200)
- K8s services exposed via `externalIPs` on ClusterIP (no MetalLB needed)
- NAT: the hypervisor host provides masquerade to external

---

## 2. Technical Implementation

### 2.1 Terraform

| Module | Purpose |
|--------|---------|
| VM creation | Creates VMs from a cloud-init template on the hypervisor (community `bpg` provider) |
| VM adoption | Metadata management on pre-existing VMs |
| `acme-cert` | Let's Encrypt wildcards via DNS-01 (Cloudflare). ECDSA P-384 |
| `pbs-s3-backup` | AWS S3 for the on-prem backup server's offsite replication |

Environment: `environments/devex/` defines all VMs, certs, and backup config.

### 2.2 Ansible Roles and Playbooks

**Key playbooks:**

| Playbook | Purpose |
|----------|---------|
| `rke2-cluster.yml` | 3-node HA RKE2 cluster with Keepalived VIP |
| `k8s-platform.yml` | Envoy Gateway, cert-manager, ESO, ArgoCD (Valkey), Rancher |
| `k8s-services.yml` | ClickHouse Operator, Strimzi Kafka, CNPG, OTel, HyperDX, FerretDB |
| `infra.yml` | DNS, DHCP, PostgreSQL 17, OpenBao |

**Platform services deployed:**

| Component | Version | Namespace |
|-----------|---------|-----------|
| Envoy Gateway | v1.4.0 | envoy-gateway-system |
| cert-manager | v1.17.2 | cert-manager |
| External Secrets | v0.17.0 | external-secrets |
| ArgoCD | v7.8.26 | argocd |
| Valkey | (latest) | argocd |
| Rancher | v2.11.1 | cattle-system |
| ClickHouse Operator | (latest) | kube-system |
| Strimzi Kafka | v0.50.1 | strimzi |
| CloudNativePG | v0.27.1 | cnpg-system |
| local-path-provisioner | v0.0.34 | local-path-storage |

### 2.3 Storage

- **Host:** ZFS pools (nvme-mirror, nvme-fast, hdd-bulk)
- **K8s:** `local-path-provisioner` creates PVs from `/data` on each node
- **NFS:** Storage VM for shared data (build caches, registry)
- **MinIO:** S3-compatible object storage

### 2.4 Certificate Management

**Dual-PKI:**
- **Internal:** OpenBao self-signed root CA (10yr) -> intermediates (VPN 5yr, TLS 5yr, K8s via cert-manager)
- **External:** Let's Encrypt via Terraform ACME module (DNS-01 Cloudflare)
- **K8s:** cert-manager with `openbao-tls-issuer` ClusterIssuer

### 2.5 DNS

CoreDNS on infra VM:
- Main zone + infrastructure includes + manual includes + dynamic (DHCP/hypervisor)
- Wildcard `*.apps.example.com -> 192.0.2.200`
- NS delegation from Cloudflare for `example.com`

### 2.6 Bootstrap Process

Phase 0 (Bare Metal) -> Phase 1 (Bootstrap VM + Terraform + Ansible) -> Phase 2 (K8s platform + services)

Cold boot: Bootstrap VM auto-starts, runs `infra-startup.yml` starting all VMs in dependency order.

---

## 3. Installation Dependencies

**Minimum hardware:** 64+ CPU cores, 256GB+ RAM, NVMe + HDD storage
**Software:** a bare-metal hypervisor, Python 3.12 + Ansible 10.x, Terraform 1.5+, Helm 3.14+
**External:** Cloudflare (DNS/certs), Let's Encrypt, Google Workspace (OIDC), GitHub

---

## 4. What DFE 2.2 Can Reuse

### 4.1 Two-Layer Architecture (Already Designed for DFE 2.2)

**Layer 1 (Base Infrastructure -- on-prem only):**
- RKE2 cluster provisioning
- Envoy Gateway with bare-metal externalIPs
- cert-manager with OpenBao PKI
- ESO with OpenBao ClusterSecretStore
- ArgoCD with Valkey
- Rancher for management UI
- local-path-provisioner

**Layer 2 (DFE Platform -- deploys on ANY K8s):**
- Strimzi Kafka (replaces MSK)
- ClickHouse Operator (replaces ClickHouse Cloud)
- CNPG PostgreSQL (replaces RDS)
- HyperDX + OTel Collector (replaces Prometheus/Grafana/CloudWatch)
- FerretDB (MongoDB API over CNPG PostgreSQL)
- KEDA for autoscaling

### 4.2 Proven Patterns

| DFE 2.2 Need | Proven in hyperi-infra |
|---------------|----------------------|
| Envoy Gateway (not nginx) | v1.4.0 with bare-metal proxy, OIDC SecurityPolicy |
| Gateway API OIDC auth | SecurityPolicy with Google OIDC, single gateway-wide policy |
| OpenBao secrets | ESO ClusterSecretStore + AppRole auth |
| OpenBao PKI certs | cert-manager ClusterIssuer |
| ArgoCD + Valkey | Already deployed (not Redis) |
| Strimzi Kafka | 3-node KRaft, SASL/SCRAM |
| ClickHouse Operator | 3-replica + 3-node Keeper |
| CNPG PostgreSQL | Single instance (DevEx), 3 for prod |
| OTel -> ClickHouse | OTel Collector deployed |
| HyperDX + FerretDB | Full deployment proven |

### 4.3 Envoy Gateway OIDC Pattern

```yaml
SecurityPolicy:
  spec:
    targetRefs:
      - group: gateway.networking.k8s.io
        kind: Gateway
        name: eg-gateway
    oidc:
      provider:
        issuer: "https://accounts.google.com"
      clientID: "<client-id>"
      clientSecret:
        name: "google-oidc-secret"  # From OpenBao via ESO
      redirectURL: "https://oidc.apps.example.com/oauth2/callback"
```

For DFE 2.2: parameterise per identity provider (Google for on-prem, cloud-native for AWS/GCP/Azure).

---

## 5. Gaps and Considerations

### 5.1 Missing for DFE 2.2

| Gap | Notes |
|-----|-------|
| KEDA deployment | Not yet deployed (listed in DFE 2.1 analysis) |
| KEDA from OTel metrics | Novel integration -- needs custom scaler or OTel->Prometheus bridge |
| Multi-environment Terraform | Single environment (devex) only |
| ArgoCD ApplicationSets | ArgoCD deployed but not yet managing DFE apps |
| Reloader | Not deployed (DFE 2.1 uses Stakater Reloader) |
| metrics-server | Not explicitly confirmed |
| Cloud provider abstraction | On-prem modules only (no EKS/GKE/AKS) |

### 5.2 KEDA from OTel Metrics (Largest Gap)

Options:
1. Custom KEDA external scaler querying ClickHouse
2. OTel Collector Prometheus exporter -> KEDA Prometheus trigger
3. Kedify OTEL Scaler (direct push, eliminates Prometheus intermediary)
4. KEDA native Kafka trigger for Kafka-based scaling (works with Strimzi SCRAM)

### 5.3 Key File References

| File | Purpose |
|------|---------|
| `docs/K8S-MIGRATION.md` | Two-layer architecture design |
| `docs/DFE-2-1-ANALYSIS.md` | DFE 2.1 component mapping |
| `docs/INFRASTRUCTURE-DESIGN.md` | Complete architecture |
| `ansible/playbooks/k8s-platform.yml` | Envoy, cert-manager, ESO, ArgoCD, Rancher |
| `ansible/playbooks/k8s-services.yml` | Operators, data services, HyperDX |
| `config/environments/devex.yml` | SSoT environment config |

---

## 6. To-Be Production Deployment (Multi-Server)

The current devex environment runs on a single hypervisor host. The production deployment will use the **same IaC codebase** (dfe-infra) across multiple physical servers.

### 6.1 Architecture Evolution

```
devex (current)                    production (to-be)
─────────────────                  ──────────────────
1 hypervisor host                  N hypervisor hosts (HA cluster)
3 K8s VMs (converged)              Dedicated control-plane + worker nodes
Single flat network                Multi-VLAN: mgmt, data, storage, tenant
local-path-provisioner             Ceph/Longhorn distributed storage
NFS from storage VM                Distributed storage (Ceph RBD/CephFS)
Single tenant                      Multi-tenant (namespace isolation)
```

### 6.2 What Stays the Same

The dfe-infra IaC codebase is designed to work identically across both environments. The differences are in:
- `terraform/environments/{env}/terraform.tfvars` — environment-specific values
- `argocd/values/{cloud}.yaml` — cloud/environment value overrides
- `versions.yaml` — same SSOT, same versions everywhere

What does NOT change between devex and production:
- Terraform modules (tf-naming, tf-secrets, tf-iam, tf-storage)
- Helm charts (all charts are parameterised via values)
- ArgoCD ApplicationSets (matrix generator reads cluster secret annotations)
- bootstrap.sh (idempotent, reads from TF outputs via bridge.py)
- OTel → ClickHouse → HyperDX observability stack
- Envoy Gateway + OIDC auth model

### 6.3 Production-Specific Additions

| Component | DevEx | Production |
|-----------|-------|------------|
| K8s nodes | 3 converged | 3+ control-plane + N workers |
| Storage | local-path + NFS | Ceph RBD (block) + CephFS (shared) |
| Networking | Single flat 192.0.2.0/24 | Multi-VLAN with Calico/Cilium |
| HA | Keepalived VIP | kube-vip or cloud LB |
| Backup | MinIO (local) | S3-compatible (MinIO cluster or cloud) |
| Tenancy sizing | dev profile | small/large profiles |
| KEDA bounds | min=1, max=5 | min=2, max=50+ |
| ClickHouse | 1 replica | 3+ replicas, sharded |

### 6.4 IaC Strategy for Multi-Server

1. **Terraform environments:** `terraform/environments/prod-{site}/` — one per physical deployment site, calling the same modules with different tfvars
2. **Ansible (from hyperi-infra):** Provisions hypervisor VMs and the RKE2 cluster — feeds into dfe-infra's Terraform + bootstrap.sh
3. **ArgoCD multi-cluster:** Each production cluster gets its own cluster secret (annotation bridge). Same ApplicationSets deploy to all clusters. Cluster-specific values via annotations.
4. **Config per tenant:** `argocd/values/` can have per-site overrides (e.g. `prod-sydney.yaml`, `prod-london.yaml`)
