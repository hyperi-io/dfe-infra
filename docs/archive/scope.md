# SCOPE — dfe-infra

**Project name:** dfe-infra
**Repo:** github.com/catinspace-au/dfe-infra (will move to hyperi-io/dfe-infra when mature)
**License:** OSS under https://github.com/hyperi-io/licensing

- This is the TF + Helm + Argo deployed SSOT for HyperI Data Fusion Engine 2.2 and later
    - All non standard root docs go to /docs
- We base this on the dfe-core https://github.com/hyperi-io/dfe-core /projects/dfe-core deployment for DFE 2.1 . It contains years of lessons leared at scale
- Step 1 is to ultrathink analyse dfe-core and produce MD file(s) with an in depth analysis of it 
    - Architecture 
    - Techical Implementation
    - Installation dependencies

- Next is dfe-engine https://github.com/hyperi-io/dfe-engine, /projects/dfe-engine This is the engine that manages the schemea, data AND some of the infrastructure at t 'big dials layer'
- Step 2 is to ultrathink analyse dfe-engine and produce MD file(s) with an in depth analysis of it 
    - Architecture 
    - Techical Implementation
    - Installation dependencies

- Next is rustlib https://github.com/hyperi-io/hyperi-rustlib, /projects/rustlib  This is a standard rust lib for all our services. BUT we only need to focus on the elements consumed and controlled by dfe-engine
- Step 3 is to ultrathink analyse dfe-engine and produce MD file(s) with an in depth analysis of it 
    - Architecture 
    - Techical Implementation
    - Installation dependencies
    - dfe-engine specific items are config, metrics

- Next is dfe-ui https://github.com/hyperi-io/dfe-ui, /projects/dfe-ui  This is the new UI for DFE and importantly directly integrates HyperDX for a lot of kibana like viz. 
- Step 4 is to ultrathink analyse dfe-engine and produce MD file(s) with an in depth analysis of it 
    - Architecture 
    - Techical Implementation
    - Installation dependencies
    - We will be using FerretDB over pg for mongodb support for HyperDx


Technical constraints
- Valkey instead fo redis for ArgoCD
- Postgresql 17 for the UI and FerretDB and other services (standaised on a single PG17 instance if possible)
- leverage the auto otel, auto config, auto logging we can rely on for our dfe-apps 
- TL-ARCHITECTURE1.mermaid contains project dependencies
- Data flow is (dfe-receiver or dfe-fetcher) -> (optional transformrs dfe-transform-*) -> (dfe-loader and optional dfe-archiver)


Then we start the work using this knoweldge base

SCOPE
- An auto deployment repo that is the SSOT for DFE 2.2 and later deployments
- Initial target is Rancher local, then AWS, then GCP then Azure (using their native tools such as EKS, secrets manager etc. but discuss what is chosen)
- Retaining the TF -> Helm -> ArgoCD approach of dfe-core BUT
    - removing the AWS specific dependnecies. 
    - metrics and logs are now otel through to clickhouse via DFE with HyperDx (no cloudwatch, prometheus, grafana)
    - cloud service swap in options are 
        - Kafka -> EKS, Confluent
    - https://github.com/hyperi-io/dfe-openvpn is an option to install for DFE Edge Steram Hub support (replaces AWS CLient VPN though tha could be retained as a swap in option though its more limited - discuss)
- Keda auto scaling
    - dfe-transform-* apps: scale-to-zero when no new data on source Kafka topic for configurable period X. KEDA Kafka consumer lag trigger detects new data and starts pods back up. Each transform is tied to a SINGLE source topic.
    - dfe-receiver, dfe-loader: scale horizontally based on dfe_scaling_pressure metric (0-1.0 composite gauge)
- Review gate: compare implementation against known-good PB-scale DFE 2.1 dfe-core deployment before each plan is marked complete
- Designed control entry points for dfe-engine. ArgoCD is the mechanism for large dial changes by users with dfe infra or admin roles
- Single source of truth auth. Ideally a simple local fallback auth and an external OIDC as the primary (e.g how elastic and other do it). oath2 preferred.
- Config driven first, then a wizard to walk through or form for AWS marketplace

DEPLOYMENT UX (the test case)
- Point to a blank AWS account (or on-prem Rancher for initial tests)
- Supply a subdomain for the DFE instance (e.g. dfe.example.com)
- Optionally supply an external OIDC provider
- Run deploy → it "just works"
- Minimum viable inputs: cloud target + domain + optional OIDC. Everything else has sensible defaults.
- This is the primary deployment and test case for validating dfe-infra

DEPLOYMENT TIERS (three paths to DFE)
- Tier 1: dfe-docker — Docker Compose for developers and basic tyre kickers. Existing project, minimal deps. No K8s needed.
- Tier 2: dfe-operator — Simple self-contained K8s operator for minimal DFE (like HyperDX has). Single CRD, `kubectl apply -f dfe.yaml` → running DFE. Good for getting started without the full deployment. Discuss: should this be the first deliverable?
- Tier 3: dfe-infra (this repo) — Full multi-cloud TF+Helm+ArgoCD deployment. Production-grade, PB-scale, multi-tenant.

Synergy: dfe-docker and dfe-operator share the same DFE service images. dfe-infra's Helm charts can be consumed by the operator. The operator is a simplified wrapper that creates the same resources dfe-infra does, but with opinionated defaults and fewer knobs.

COMPONENT INDEPENDENCE (container-per-component)
- Each DFE service (receiver, loader, archiver, fetcher, transform-*, engine, ui) has its own container image and Helm chart
- Any component can be deployed in isolation — e.g. just dfe-receiver + dfe-loader for a minimal pipeline, or just dfe-receiver as a standalone ingest endpoint
- This enables users/customers to adopt individual components without the full platform
- Each image is independently versioned, built, and pushed to the registry
- The full dfe-infra deployment is the composition of all components; individual components are first-class citizens

NODE SCALING (capacity autoscaling)
- KEDA = pod scaling (horizontal). Node scaling = separate concern.
- On-prem: Cluster API (CAPI) + the community CAPI infrastructure provider for the on-prem hypervisor. Autoscaler watches pending pods → provisions VMs via the hypervisor API → cloud-init → RKE2 join. ~2 min.
- AWS: Karpenter (preserved from dfe-core 2.1)
- GCP/Azure: native node autoscalers
- DevEx (now): fixed 3 nodes, overprovisioned (72 cores, 192GB). No autoscaling until production.
- New TF module: tf-node-autoscaler (CAPI for on-prem, Karpenter for AWS)


LICENSE REVIEW
- This project is released as OSS. Before each release, audit all dependency licenses (Terraform providers, Helm chart dependencies, container images, npm/cargo/pypi packages) against the approved license policy at https://github.com/hyperi-io/licensing
- Flag any GPL/AGPL/SSPL dependencies — these require legal sign-off before inclusion
- Document approved licenses in docs/license-review.md per release cycle

INITIAL DELIVERABLES
- Project name: dfe-infra ✓
- Architecture and implementation plan documents
