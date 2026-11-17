<!--
Project:   dfe-infra
File:      docs/deployment/rke2.md
Purpose:   A primer on RKE2 - what it is, how it works with DFE, and why DFE
           uses and heavily standardises on it (incl. the federal compliance baseline).
Language:  Markdown
License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# RKE2 - a primer (the DFE reference distribution)

Status: dfe-infra deploys onto an RKE2 cluster and doesn't build one. Provisioning RKE2 where a customer has no cluster, with its hardened profile and self-hosted OIDC issuer, is design only and not built (https://github.com/hyperi-io/dfe-infra/issues/1123).

DFE targets any CNCF-conformant Kubernetes. RKE2 is the reference distribution. RKE2 on-prem and Amazon EKS are the tested targets, and the aws root builds the EKS cluster. Azure and GCP have values overlays (`argocd/values/azure.yaml`, `gcp.yaml`) and no cloud root, so a deployment there brings a cluster and sets the values by hand.

This page covers RKE2: what it is, how it sits under DFE, and why it's our reference.

## What is RKE2

RKE2 (Rancher Kubernetes Engine 2, originally "RKE Government") is SUSE/Rancher's security-focused, CNCF-conformant Kubernetes distribution. It keeps K3s's easy single-binary install but rebuilds the internals for enterprise security and strict upstream conformance:

- One `rke2` binary, run as `rke2-server` (control plane) or `rke2-agent` (worker).
- containerd (not Docker) and embedded etcd (not SQLite).
- Hardened from first boot: CIS defaults, audit logging on, anonymous auth off, one-flag secrets-at-rest encryption.
- FIPS-validated crypto (Go + BoringCrypto).

It isn't K3s, which runs the whole control plane in one process on SQLite and is built for the edge. RKE2 is the data-centre and regulated-workload sibling, and many shops run both.

## How RKE2 works with DFE

RKE2 runs each control-plane component (apiserver, scheduler, controller-manager, etcd) as a static pod in `kube-system`. That's the upstream `kubeadm` model, so you can see and debug each one with `kubectl`, unlike K3s's single process. A server node runs the control plane. Agent nodes run the kubelet and CNI and host the workloads.

DFE sits on top of whichever cluster you have. It assumes a conformant, running Kubernetes cluster and deploys onto it. Where there's no cluster, RKE2 is how a bare machine or cloud VM becomes one. DFE's layers run as workloads on the cluster, and Rancher or the cloud manages the control plane under them.

```mermaid
---
config:
  theme: base
  look: neo
  fontFamily: Inter, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif
  flowchart:
    curve: linear
    nodeSpacing: 40
    rankSpacing: 64
    padding: 18
    wrappingWidth: 280
    htmlLabels: true
  elk:
    preset: modelOrder
  themeVariables:
    fontSize: 15px
    lineColor: "#64748B"
    primaryTextColor: "#0F172A"
    clusterBorder: "#0072B2"
    edgeLabelBackground: "#FFFFFF"
  themeCSS: >-
    p{margin:0}
    .node[data-look] rect,.node[data-look] path,.node[data-look] polygon,.node[data-look] .outer-path{filter:drop-shadow(0 1px 2px rgba(15,23,42,.10)) drop-shadow(0 6px 14px rgba(15,23,42,.10))}
    .node rect{rx:10px;ry:10px}
    .cluster rect{rx:16px;ry:16px}
    .node .label div{min-width:150px}
    .node.decision .label div{min-width:0}
    .cluster[data-look] rect{filter:none}
    .nodeLabel{line-height:1.35}
    .nodeLabel b{font-weight:600;font-size:15px;letter-spacing:-0.01em}
    .nodeLabel span{font-size:12.5px;font-weight:400;color:#334155}
    .cluster-label span.nodeLabel{display:inline-block;background:#FFFFFF;color:#0B2A3F;border:1.5px solid #0072B2;border-radius:999px;padding:2px 12px;font-size:11.5px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}
    .flowchart-link{stroke-width:1.75px}
    .labelBkg{background:transparent!important}
    span.edgeLabel{display:inline-block;background:#FFFFFF!important;color:#1E293B;border:1px solid #94A3B8;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:500}
    span.edgeLabel p{background:transparent!important}
    .label-icon{width:20px;height:20px;vertical-align:-4px;margin-right:6px}
    .ingest .label-icon{color:#0E8A7D}
    .xform .label-icon{color:#7B4FB0}
    .load .label-icon{color:#0072B2}
    .bus .label-icon{color:#A8517F}
    .store .label-icon{color:#00785A}
    .ui .label-icon{color:#D9480F}
    .ext .label-icon{color:#64748B}
---
flowchart LR
    accTitle: L4 Detail -- DFE on an RKE2 cluster
    accDescr: Rancher, or the cloud on a managed cluster, manages the control plane. The control plane schedules DFE's three layers as workloads: bootstrap's Layer 0, the Layer 1 backing services and the Layer 2 apps.

    owner("<b>Rancher or the cloud</b>")
    cp("<b>Control plane</b><br/><span>etcd, apiserver</span>")

    subgraph dfe["DFE workloads"]
        layer0("<b>Layer 0</b><br/><span>cert-manager, ESO, Argo CD</span>")
        layer1("<b>Layer 1</b><br/><span>backing services</span>")
        layer2("<b>Layer 2</b><br/><span>DFE apps</span>")
        layer0 ~~~ layer1 ~~~ layer2
    end

    owner -->|manages| cp
    cp -->|schedules| dfe

    class owner ext
    class cp,layer0,layer1,layer2 part
  classDef ext fill:#F4F6F8,stroke:#94A3B8,stroke-width:1.5px,stroke-dasharray:5 4,color:#0F172A
  classDef ingest fill:#FFFFFF,stroke:#0E8A7D,stroke-width:3px,color:#0F172A
  classDef xform fill:#FFFFFF,stroke:#7B4FB0,stroke-width:3px,color:#0F172A
  classDef load fill:#FFFFFF,stroke:#0072B2,stroke-width:3px,color:#0F172A
  classDef bus fill:#FFFFFF,stroke:#A8517F,stroke-width:3px,color:#0F172A
  classDef store fill:#FFFFFF,stroke:#00785A,stroke-width:3px,color:#0F172A
  classDef ui fill:#FFFFFF,stroke:#D9480F,stroke-width:3px,color:#0F172A
  classDef decision fill:#FFFFFF,stroke:#9A6700,stroke-width:3px,color:#0F172A
  classDef error fill:#FFFFFF,stroke:#C2410C,stroke-width:3px,color:#0F172A
  classDef control fill:#FFFFFF,stroke:#334155,stroke-width:3px,color:#0F172A
  classDef part fill:#FFFFFF,stroke:#94A3B8,stroke-width:1.5px,color:#0F172A
  style dfe fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 1. L4 Detail -- DFE on an RKE2 cluster. The dashed box is external.* Up: [Deploying DFE with dfe-infra](README.md).

DFE never manages RKE2's control plane. Rancher/RKE2 own etcd, upgrades and hardening, and DFE deploys into the cluster they provide. What differs by cluster type:

- On an RKE2 cluster you bring, the hardening is whatever its operator configured. The design (#1123) provisions RKE2 with its hardened profile for a FIPS, CIS and STIG-hardened baseline, which wouldn't cover DFE's workloads, assessed separately.
- On RKE2 on-prem today, pods have no cloud identity, and DFE's secrets come from OpenBao through ESO ([portability-matrix.md](portability-matrix.md)). The design adds a self-hosted OIDC issuer so RKE2 on a cloud's VMs gets cloud identity (AWS IRSA, GCP WIF, Azure Entra). Nothing builds it yet.
- On managed targets, the cloud's mechanism gives pods their identity: EKS Pod Identity, Azure Workload Identity, GKE Workload Identity.

## Why RKE2 is the reference

DFE is a product suite other organisations deploy. Where we provision the cluster, the same base has to land identically on-prem, air-gapped and on bare IaaS.

Where there's no cluster, the design puts RKE2 on every kind of host. On a managed cluster, DFE deploys onto what the cloud runs.

```mermaid
---
config:
  theme: base
  look: neo
  fontFamily: Inter, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif
  flowchart:
    curve: linear
    nodeSpacing: 40
    rankSpacing: 64
    padding: 18
    wrappingWidth: 280
    htmlLabels: true
  elk:
    preset: modelOrder
  themeVariables:
    fontSize: 15px
    lineColor: "#64748B"
    primaryTextColor: "#0F172A"
    clusterBorder: "#0072B2"
    edgeLabelBackground: "#FFFFFF"
  themeCSS: >-
    p{margin:0}
    .node[data-look] rect,.node[data-look] path,.node[data-look] polygon,.node[data-look] .outer-path{filter:drop-shadow(0 1px 2px rgba(15,23,42,.10)) drop-shadow(0 6px 14px rgba(15,23,42,.10))}
    .node rect{rx:10px;ry:10px}
    .cluster rect{rx:16px;ry:16px}
    .node .label div{min-width:150px}
    .node.decision .label div{min-width:0}
    .cluster[data-look] rect{filter:none}
    .nodeLabel{line-height:1.35}
    .nodeLabel b{font-weight:600;font-size:15px;letter-spacing:-0.01em}
    .nodeLabel span{font-size:12.5px;font-weight:400;color:#334155}
    .cluster-label span.nodeLabel{display:inline-block;background:#FFFFFF;color:#0B2A3F;border:1.5px solid #0072B2;border-radius:999px;padding:2px 12px;font-size:11.5px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}
    .flowchart-link{stroke-width:1.75px}
    .labelBkg{background:transparent!important}
    span.edgeLabel{display:inline-block;background:#FFFFFF!important;color:#1E293B;border:1px solid #94A3B8;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:500}
    span.edgeLabel p{background:transparent!important}
    .label-icon{width:20px;height:20px;vertical-align:-4px;margin-right:6px}
    .ingest .label-icon{color:#0E8A7D}
    .xform .label-icon{color:#7B4FB0}
    .load .label-icon{color:#0072B2}
    .bus .label-icon{color:#A8517F}
    .store .label-icon{color:#00785A}
    .ui .label-icon{color:#D9480F}
    .ext .label-icon{color:#64748B}
---
flowchart TB
    accTitle: L4 Detail -- Where RKE2 runs
    accDescr: Where there is no cluster, RKE2 is the one distribution on on-prem hardware, AWS EC2, GCP Compute and Azure VMs. On a managed cluster, EKS, AKS or GKE Standard, DFE deploys onto what the cloud runs.

    subgraph rke2["RKE2, where there is no cluster"]
        direction LR
        onprem("<b>On-prem, air-gapped, edge</b>")
        ec2("<b>AWS EC2</b>")
        gce("<b>GCP Compute</b>")
        azvm("<b>Azure VMs</b>")
        onprem ~~~ ec2 ~~~ gce ~~~ azvm
    end

    subgraph managed["Managed, DFE deploys onto"]
        direction LR
        eks("<b>EKS</b>")
        aks("<b>AKS</b>")
        gke("<b>GKE Standard</b>")
        eks ~~~ aks ~~~ gke
    end

    rke2 ~~~ managed

    class onprem,ec2,gce,azvm part
    class eks,aks,gke ext
  classDef ext fill:#F4F6F8,stroke:#94A3B8,stroke-width:1.5px,stroke-dasharray:5 4,color:#0F172A
  classDef ingest fill:#FFFFFF,stroke:#0E8A7D,stroke-width:3px,color:#0F172A
  classDef xform fill:#FFFFFF,stroke:#7B4FB0,stroke-width:3px,color:#0F172A
  classDef load fill:#FFFFFF,stroke:#0072B2,stroke-width:3px,color:#0F172A
  classDef bus fill:#FFFFFF,stroke:#A8517F,stroke-width:3px,color:#0F172A
  classDef store fill:#FFFFFF,stroke:#00785A,stroke-width:3px,color:#0F172A
  classDef ui fill:#FFFFFF,stroke:#D9480F,stroke-width:3px,color:#0F172A
  classDef decision fill:#FFFFFF,stroke:#9A6700,stroke-width:3px,color:#0F172A
  classDef error fill:#FFFFFF,stroke:#C2410C,stroke-width:3px,color:#0F172A
  classDef control fill:#FFFFFF,stroke:#334155,stroke-width:3px,color:#0F172A
  classDef part fill:#FFFFFF,stroke:#94A3B8,stroke-width:1.5px,color:#0F172A
  style rke2 fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style managed fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 2. L4 Detail -- Where RKE2 runs. Dashed boxes are run by the cloud.* Up: [Deploying DFE with dfe-infra](README.md).

One distro means one Kubernetes version matrix, one CNI/CSI, one hardening profile, one identity pattern, one debugging surface and one set of docs. The alternative is three divergent managed distros, because EKS, AKS and GKE differ on CNI, add-ons, auth and upgrade cadence. RKE2 runs on bare metal, any cloud's VMs and the edge, which is the widest customer reach, and Rancher handles the fleet lifecycle (etcd snapshots, rolling upgrades). The cost, once we provision clusters (#1123), is that we'd own etcd backup/restore, node OS patching, control-plane upgrades and the per-cloud OIDC webhook. That's work, but it's bounded and single-path.

RKE2 meets the federal and regulated requirements, which is a large part of the "why". Verify version specifics with Rancher Government per deployment:

- FIPS 140-2 validated crypto (Go + BoringCrypto, CMVP certificate 4691), the first NIST-FIPS-140-2-validated Kubernetes distribution. The STIG needs 140-2 or 140-3 modules, so confirm 140-3 module status for your build.
- DISA STIG. RKE2 has an official DISA-published STIG (the Rancher Government Solutions RKE2 STIG), derived from NIST SP 800-53. That's what lets the DoD run it on network systems.
- CIS Kubernetes Benchmark. It's hardened by default (`cis` profile, `kube-bench`-scannable).
- FedRAMP (cloud) and DISA STIG (on-prem) both need FIPS crypto, audit logging, strict access control and (often) air-gap. RKE2 meets them when configured correctly.
- Iron Bank (USAF hardened registry) and SELinux enforcing.

A few STIG controls need infrastructure beyond the distro: MFA / CAC-PIV, SIEM audit forwarding and an air-gapped registry. The deploy environment and the deployment's OIDC IdP supply those.

See also:

- Crypto standard: [cnsa-standard.md](../cnsa-standard.md)
- Official: RKE2 architecture (https://docs.rke2.io/architecture), FIPS support (https://docs.rke2.io/security/fips_support), the DISA RKE2 STIG (NIST NCP checklist 1040).
