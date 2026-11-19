# Deployment portability matrix

DFE is one product deployed into many estates. Most components sit behind a config dial, so a deployment swaps them without a code change. A dial is an existing k8s abstraction or a chart mode, never a fork. A few components are fixed in-cluster and have no dial: the Envoy edge, the identity authority and the document store. This page shows how each target sets the dials. The definitions (what each dial turns) live in [../architecture.md](../architecture.md) "Swappable components", and that stays the SSoT.

RKE2 on-prem is the reference distribution and the tested path. The cloud columns use the same charts and dials with different values and credentials. RKE2 and AWS are built and tested. Azure and GCP are values overlays alone (`argocd/values/azure.yaml`, `gcp.yaml`) with no cloud root, so a deployment there brings a cluster and sets the values by hand.

## The invariant core vs the swappable edge

The Envoy edge, the identity authority, the document store and the apps run in-cluster on every target, and the parts around them swap per target.

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
    nodeBorder: "#334155"
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
    .icon-shape path[stroke-width="0"][fill="none"]{fill:#FFFFFF;stroke:#FFFFFF;stroke-width:12px;stroke-linejoin:round}
    .icon-shape p{background:transparent!important;color:#0F172A}
    .ingest .label-icon{color:#0E8A7D}
    .xform .label-icon{color:#7B4FB0}
    .load .label-icon{color:#0072B2}
    .bus .label-icon{color:#A8517F}
    .store .label-icon{color:#00785A}
    .ui .label-icon{color:#D9480F}
    .ext .label-icon{color:#64748B}
---
flowchart LR
    accTitle: L4 Detail -- Fixed and swappable components
    accDescr: DNS, secrets, the CA and the L4 LoadBalancer swap per target. Envoy Gateway, dfe-engine, the DocumentDB store and the dfe apps run in-cluster on every target. Kafka, ClickHouse and the object store run in-cluster or as a managed endpoint.

    subgraph edge["Swappable per target"]
        direction TB
        dns("<b>DNS</b>")
        secrets("<b>Secrets</b>")
        ca("<b>CA, TLS</b>")
        lb("<b>L4 LoadBalancer</b>")
        dns ~~~ secrets ~~~ ca ~~~ lb
    end

    subgraph incluster["In-cluster, every target"]
        direction TB
        gateway("<b>Envoy Gateway</b>")
        engine("<b>dfe-engine</b>")
        docdb[("<b>DocumentDB</b>")]
        apps("<b>dfe apps, UIs</b>")
        gateway ~~~ engine ~~~ docdb ~~~ apps
    end

    subgraph data["In-cluster or managed"]
        direction TB
        kafka[("<b>Kafka</b>")]
        clickhouse[("<b>ClickHouse</b>")]
        objstore[("<b>Object store</b>")]
        kafka ~~~ clickhouse ~~~ objstore
    end

    edge ~~~ incluster ~~~ data

    class dns,secrets,ca,lb ext
    class gateway,apps part
    class engine control
    class docdb,clickhouse,objstore store
    class kafka bus
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
  style edge fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style incluster fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style data fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 1. L4 Detail -- Fixed and swappable components.* Up: [Deploying DFE with dfe-infra](README.md).

One cloud L4 LoadBalancer backs the in-k8s Envoy Gateway, which does ALL the L7 routing, and there's no cloud L7 (ALB/App Gateway). The receiver's ingest door and the VPN tunnel are L4 doors of their own (below). dfe-engine (the identity authority), the Envoy edge and the dfe-hyperdx DocumentDB store are always in-cluster. Kafka, ClickHouse and object storage can be in-cluster or a managed endpoint.

## The matrix

| Component | The dial | On-prem RKE2 | AWS | Azure | GCP |
|---|---|---|---|---|---|
| Kubernetes | vanilla-cluster contract (preflight) | RKE2 (standard) | EKS | AKS | GKE |
| External DNS | external-dns provider / estate record | estate CoreDNS record | Route53 | Azure DNS | Cloud DNS |
| Secrets | ESO ClusterSecretStore | OpenBao | Secrets Manager | Key Vault | Secret Manager |
| Private CA / TLS | cert-manager ClusterIssuer (+ external issuer) | OpenBao PKI | ACME or AWS PCA | ACME or Key Vault | ACME or Google CAS |
| Kafka | chart mode + `kafka.bootstrapServers` | Strimzi / Redpanda* | MSK / Confluent Cloud / Redpanda Cloud, each on AWS** | Confluent Cloud on Azure** | Confluent Cloud / Redpanda Cloud, each on GCP** |
| ClickHouse | `clickhouse.mode` + `.host` | in-k8s operator | CH Cloud (pref) / in-k8s | CH Cloud (pref) / in-k8s | CH Cloud (pref) / in-k8s |
| Edge LoadBalancer | `envoyGateway.service` type=LoadBalancer | MetalLB | NLB | Azure LB | GCP LB |
| Object store | S3-compatible endpoint + creds | MinIO / Ceph RGW | S3 | Blob (S3 API) | GCS (S3 API) |
| Container registry | `global.registry`, plus a pull secret for a private mirror | Harbor | ECR | ACR | GAR |
| Image pull cache | containerd mirror per upstream | not built | ECR pull-through cache | ACR artifact cache (not built) | AR remote repos (not built) |
| StorageClass / CSI | `storageClass` (empty -> cluster default) | local-path / Longhorn | EBS | Azure Disk | PD |
| Workload identity | ServiceAccount annotations | static creds (OpenBao) | EKS Pod Identity (no annotation) | Azure Workload Identity | GKE Workload Identity |
| etcd-at-rest KMS | cluster EncryptionConfiguration provider | secretbox / aescbc | KMS | Key Vault KMS | Cloud KMS |
| Document store | documentdb chart, a DocumentDB CR on the DocumentDB operator's CloudNativePG (always in-cluster, Kubernetes 1.35+, or 1.34 with the ImageVolume feature gate) | 2 instances, gateway in each | " | " | " |
| Identity authority | dfe-engine account store (always in-cluster) | dfe-engine | dfe-engine | dfe-engine | dfe-engine |
| Federated login | `oidc.providers` external OIDC (opt-in) | any OIDC IdP | Entra / Okta / ... | Entra | Google / ... |

\* Redpanda on-prem is behind a licence gate.

\** A managed backend is offered as its instance on the deployment's cloud alone, and the plan rejects any other: [managed-backends.md](managed-backends.md).

## Notes that matter

- Dumb L4 LoadBalancers. The web plane's one LB is the `Service type=LoadBalancer` fronting the gateway (MetalLB provides it on-prem). The receiver's ingest door is a LoadBalancer of its own on cloud flavours (`exposure.groups.ingest.door: loadbalancer` in `argocd/values/common.yaml`) and the gateway on-prem, and a deployed tunnel takes one more ([edge-vpn.md](edge-vpn.md)). We don't use cloud L7 (routing, WAF, OIDC). Envoy owns L7 in-cluster, so behaviour is identical everywhere. Cloud-specific LB tuning rides `envoyGateway.service.annotations`.
- Workload identity is how the cloud credentials work. ESO, external-dns and the cloud LB controller reach their cloud APIs through workload identity: EKS Pod Identity on AWS, which needs no annotation, and Workload Identity annotated onto the ServiceAccount on Azure and GCP. On-prem there's no cloud API, so credentials come from OpenBao through ESO. Nothing hardcodes a static cloud key.
- etcd-at-rest belongs to the cluster, not the app. dfe-engine (and every `secrets` consumer) inherits whatever the cluster's EncryptionConfiguration uses: cloud KMS on managed clusters, secretbox/aescbc on-prem. No app couples to a KMS.
- Data services are in-cluster OR managed, per deployment. Kafka and ClickHouse default to in-cluster (Strimzi, the CH operator) and swap to a managed endpoint when you point the chart's `*.host` or `bootstrapServers` at it. The DocumentDB store's PostgreSQL stays in-cluster, because it's the dfe-hyperdx store and not a general PG.
- ClickHouse on cloud: ClickHouse Cloud is PREFERRED, and in-k8s ClickHouse on the operator is equally ACCEPTABLE. `clickhouse.mode` supports both everywhere, so it's a per-deployment call.
- A chart never assumes anything cloud-specific. A component reachable through one provider alone is a bug ([architecture.md](../architecture.md)).
- Declaring an external dependency configures its clients' egress rules. The baseline is default-deny egress, so `network-policies` derives an egress allow for each external dependency the deployment declares: `clickhouse.mode: external` opens the ClickHouse clients on the ClickHouse ports, `kafka.mode: external` the Kafka clients, and likewise for an external OIDC issuer, secret backend, index server or OTel destination. Without it, a pod pointed at the datastore would crashloop with nothing in the policy set to explain why. Each purpose gets the ports that dependency speaks and no others, and any purpose pins to a known endpoint CIDR (`externalEgress.<purpose>.allowCIDRs`) where one is fixed.

## Node autoscaling

It's a pools interface with one renderer per cloud. The resolver writes a pool class per workload, and `helm/charts/node-pools` renders the classes for the cluster's node autoscaler. That's Karpenter on AWS. Azure and GCP have no renderer in this release. On-prem runs a static node pool and renders no pool object. [../autoscaling.md](../autoscaling.md) has the per-target table.
