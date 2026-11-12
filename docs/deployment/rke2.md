<!--
Project:   dfe-infra
File:      docs/RKE2.md
Purpose:   A primer on RKE2 - what it is, how it works with DFE, and why DFE
           uses and heavily standardises on it (incl. the federal compliance baseline).
Language:  Markdown
License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# RKE2 - a primer (the DFE reference distribution)

DFE targets any CNCF-conformant Kubernetes. RKE2 is the reference distribution, and the one we provision when a customer has no cluster (on-prem, bare IaaS). RKE2 on-prem and Amazon EKS are the tested targets. Azure and GCP have values overlays (`argocd/values/azure.yaml`, `gcp.yaml`) and no cloud root, so a deployment there brings its own cluster and sets the values by hand.

This page covers RKE2: what it is, how it sits under DFE, and why it's our reference.

## What is RKE2

RKE2 (Rancher Kubernetes Engine 2, originally "RKE Government") is SUSE/Rancher's security-focused, fully CNCF-conformant Kubernetes distribution. It keeps K3s's easy single-binary install but rebuilds the internals for enterprise security and strict upstream conformance:

- One `rke2` binary, run as `rke2-server` (control plane) or `rke2-agent` (worker).
- containerd (not Docker) and embedded etcd (not SQLite).
- Hardened from first boot: CIS defaults, audit logging on, anonymous auth off, one-flag secrets-at-rest encryption.
- FIPS-validated crypto (Go + BoringCrypto).

It isn't K3s, which runs the whole control plane in one process on SQLite and is built for the edge. RKE2 is the data-centre and regulated-workload sibling, and many shops run both.

## How RKE2 works with DFE

RKE2 runs each control-plane component (apiserver, scheduler, controller-manager, etcd) as a static pod in `kube-system`. That's the upstream `kubeadm` model, so you can see and debug each one with `kubectl`, unlike K3s's single process. A server node runs the control plane. Agent nodes run the kubelet and CNI and host the workloads.

DFE sits on top of whichever cluster you have. It assumes a conformant, running Kubernetes cluster and deploys onto it. Where there's no cluster, RKE2 is how a bare machine or cloud VM becomes one (the "assume a bare cluster" Layer-0 baseline). DFE's own two layers run entirely as workloads on the cluster:

```mermaid
flowchart TB
  apps["DFE apps: engine / ui / receiver / loader / ..."]
  data["data services: ClickHouse / Kafka / Postgres / dfe-hyperdx"]
  l1["DFE Layer 1: cert-manager / ESO / ArgoCD / Envoy Gateway"]
  rke2["Kubernetes cluster: RKE2 (reference) or EKS / AKS / GKE Standard"]
  nodes["nodes: control plane + workers"]
  apps --> data
  data --> l1
  l1 -->|all run as workloads on| rke2
  rke2 --> nodes
```

DFE never manages RKE2's control plane. Rancher/RKE2 own etcd, upgrades and hardening, and DFE deploys into the cluster they provide. What differs by cluster type:

- On RKE2, the hardened profile gives you a FIPS, CIS and STIG-hardened cluster baseline. DFE's own workloads are assessed in their own right, and the cluster baseline doesn't cover them.
- On RKE2, the self-hosted OIDC issuer gives pods their cloud identity (AWS IRSA, GCP WIF, Azure Entra). So DFE's secrets and ESO flow works the same wherever RKE2 runs.
- On managed targets, the cloud's own mechanism gives pods their identity: EKS Pod Identity, Azure Workload Identity, GKE Workload Identity ([portability-matrix.md](portability-matrix.md)).

## Why RKE2 is the reference

DFE is a product suite other organisations deploy. Where we provision the cluster, the same base has to land identically on-prem, air-gapped and on bare IaaS.

```mermaid
flowchart LR
  rke2["RKE2<br/>(one hardened distro, where we provision the cluster)"]
  rke2 --> onprem["on-prem / bare-metal / air-gapped / edge"]
  rke2 --> aws["AWS (EC2)"]
  rke2 --> gcp["GCP (Compute)"]
  rke2 --> az["Azure (VM)"]
  managed["EKS / AKS / GKE Standard<br/>(DFE deploys onto)"]
```

One distro means one Kubernetes version matrix, one CNI/CSI, one hardening profile, one identity pattern, one debugging surface and one set of docs. The alternative is three divergent managed distros, because EKS, AKS and GKE differ on CNI, add-ons, auth and upgrade cadence. RKE2 runs on bare metal, any cloud's VMs and the edge, which is the widest customer reach, and Rancher handles the fleet lifecycle (etcd snapshots, rolling upgrades). The cost is that, on clusters we provision, we own etcd backup/restore, node OS patching, control-plane upgrades and the per-cloud OIDC webhook. That's real work, but it's bounded and single-path.

RKE2 also meets the federal and regulated requirements, which is a large part of the "why". Verify version specifics with Rancher Government per deployment (as of 2026-07):

- FIPS 140-2 validated crypto (Go + BoringCrypto, CMVP certificate 4691), the first NIST-FIPS-140-2-validated Kubernetes distribution. The STIG requires 140-2 or 140-3 modules, so confirm 140-3 module status for your build.
- DISA STIG. RKE2 is the only Kubernetes distribution with an official DISA-published STIG (Rancher Government Solutions RKE2 STIG, V2R3), derived from NIST SP 800-53. That's what lets the DoD run it on network systems.
- CIS Kubernetes Benchmark. It's hardened by default (`cis` profile, `kube-bench`-scannable).
- FedRAMP (cloud) and DISA STIG (on-prem) both need FIPS crypto, audit logging, strict access control and (often) air-gap. RKE2 meets them when configured correctly.
- Iron Bank (USAF hardened registry) and SELinux enforcing.

A few STIG controls need infrastructure beyond the distro: MFA / CAC-PIV, SIEM audit forwarding and an air-gapped registry. DFE's auth layer (OIDC and the AuthZEN/CEL PDP) and the deploy environment supply those.

See also:

- Crypto standard: `docs/cnsa-standard.md`
- Official: RKE2 architecture (`docs.rke2.io/architecture`), FIPS support
  (`docs.rke2.io/security/fips_support`), the DISA RKE2 STIG (NIST NCP checklist 1040).
