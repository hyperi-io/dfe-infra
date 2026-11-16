# Cloud and edge values

The cloud files and the edge flavour files in `argocd/values/` carry a pointer here and little else. This page is the reasoning behind their keys. Shared facts are in [values-shared.md](values-shared.md).

## Cloud files

The cloud files are one deploy's sample values, and the charts and appsets hardcode none of it. A deployment supplies a domain, vault and storage through the cluster-secret annotations that `bootstrap.sh` sets from the private `.env`, or through its deploy-repo overlay, which layers last. `aws.yaml`, `azure.yaml` and `gcp.yaml` exist even when nearly empty so the layer 2 `valueFiles` resolution succeeds on those targets.

- Each file keeps what a chart outside the edge module reads. The edge module's keys live in `edge-<flavour>.yaml`.
- `global.registry` is unset on the clouds because the dial's `registry:` field is the one source. It reaches the charts as the cluster-secret annotation, not through the `dfe-ops --registry` flag, which stops at the version-set render.
- `aws.yaml` keeps `oidc.enabled: true` because two charts outside the edge module read it. Kafbat degrades OAUTH2 to its break-glass `LOGIN_FORM` without it, and dfe-engine stops rendering each provider's secret env vars. It sets the switch and no provider, so a deployer who turns `exposure.groups.admin` back on still needs an `oidc.providers` entry.
- `aws.yaml` sets `clickhouse.objectStore.usePodIdentity` because the object store is the bucket the cluster module creates, reached through EKS Pod Identity. With no static key the chart's ExternalSecret and credential env vars stay off.
- `azure.yaml` uses `managed-csi-premium`. `managed-premium` is the retired in-tree name and binds nothing (#294). The retired name was Premium SSD and `managed-csi` is Standard SSD, which Keeper's continuous fsync notices.
- `local.yaml` has no LoadBalancer provisioner, so the gateway's LB Service stays pending and the receiver's load balancer would never get an address. Ingest goes through the gateway on `receiver.{domain}` with `door: gateway`, and `local-dfe.yaml` and `rancher.yaml` set the same door.
- `local.yaml` drops `grpc` to `exposed: false`. The gateway routes HTTP, and the receiver render fails on an exposed grpc, which is the point of that guard. Helm replaces a list instead of merging it, so every default listener is restated, `pushgrpc` included, or dfe-fetcher has nothing to push to in the no-kafka profile. culvert reads the same key, so a tunnel on this flavour restates `wireguard` and `openvpn-udp` in the tunnel's instance values file.
- `local.yaml` and `local-dfe.yaml` run DFE's single-mode ClickHouse with Keeper embedded. `local-dfe.yaml` archives to the pod's local disk, which proves the landing topics reach the archiver with no object store. A deployment that keeps archives sets `s3.*`.

## Edge flavour files

`edge-aws.yaml`, `edge-azure.yaml`, `edge-gcp.yaml` and `edge-onprem.yaml` are the edge module's tier table for `helm/edge/gateway` and culvert's thin chart. `argocd/appsets/layer2-edge.yaml` layers each after its cloud file, and no other appset reads it. Tier 1 is on by default and adds nothing beyond the cluster's compute. Tier 2 is a costed opt-in in the deployer's overlay, priced as a T-shirt bucket and never a rate ([aws.md](aws.md#how-costs-are-described)). Tier 3 is absent by design, and Kafka, ClickHouse, Keeper, PostgreSQL, OpenBao and the Kubernetes API get no door on any flavour. Azure and GCP have no tofu root, so those flavours are values-only.

- `tls.issuerName: dfe-internal-ca` on every flavour. The chart default (`dfe-letsencrypt`) renders a ClusterIssuer only when `tls.acme.email` or `tls.vault.server` is set. With neither, `dfe-wildcard-tls` is never issued, Envoy Gateway drops the https listener and the load balancer has no 443. The internal CA issues instantly with no DNS-01 and no external credential, and clients must trust `dfe-internal-ca-tls` in namespace `cert-manager`. A deployment sets `tls.acme.*` or `tls.vault.*` (OpenBao PKI) in its deploy repo for a browser-trusted leaf, and AWS rejects `tls.vault.*` because it runs no OpenBao.
- `envoyGateway.service.internetFacing` fences every web route to its exposure group's allow list, which rejects every address until a deployment names who may reach it. GKE and AKS make a LoadBalancer Service internet-facing unless annotated internal, and Envoy Gateway's default `externalTrafficPolicy: Local` keeps the client address the fence reads.
- `exposure.groups.admin.enabled: false` on the clouds, because the gateway Service is internet-facing. The admin UIs stay off the public load balancer until an overlay turns the group on, which the chart rejects without an admin allow list or `allowPublic`.
- `pki.mode: external` on the clouds, because local mode mints the CA inside the pod and a restart without a durable volume invalidates every client config issued. External is the one mode with a tls-crypt-v2 key and a CRL that survives a rebuild. The deployment names the Secret with `ca.crt`, `server.crt` and `server.key` in its culvert instance values, and the chart fails to render without it. On-prem keeps the chart's `local` mode, where the admin is on the LAN.
- `persistence.enabled` is set per cloud in `layer2-edge.yaml`, not in these files, because charts outside the module read the key. culvert writes revocations and WireGuard peers into its PKI directory, so a restart without a volume loses both.

## Edge on-prem, GCP and Azure

- On-prem, the tunnel takes a real LoadBalancer addressed by MetalLB. There is no per-byte charge to dodge and the node's NIC is the limit. `loadBalancerSourceRanges` empty is `0.0.0.0/0` by design, because a fleet dialling from anywhere is the normal case.
- On GCP and Azure the tunnel keeps culvert's LoadBalancer, so it bills the cloud's per-GB rate on the tunnel's bytes until the AWS NodePort override is built. The admin-peer reach-back is missing there. It needs ranges from a toolbox module that has no root, and `peers.classes.admin.enabled: true` with no `adminCIDRs` is a switch that reports nothing either way.

## AWS edge

The gateway Service annotations are for the AWS Load Balancer Controller.

- `aws-load-balancer-type: external`, because the controller reconciles `nlb-ip` and `external`. `nlb` is read by nothing and left the in-tree cloud provider making the load balancer.
- `nlb-target-type: ip`, so a request reaches Envoy in one hop with no node-port hop that hides the client address.
- `scheme: internet-facing`, because the UIs' users are outside the VPC. Everything else DFE runs stays private.
- `preserve_client_ip.enabled=true` makes the client Envoy's TCP peer, which is what the fence judges. Without it every request comes from the load balancer's address, and the CIDR filter and rate limit do nothing. The NLB writes no `X-Forwarded-For` and none is believed.
- No `aws-load-balancer-security-groups`. Left absent, the controller creates a frontend security group at creation and opens it to `loadBalancerSourceRanges`. Creation is the one time an NLB can be given one, so an NLB born without a security group never gains one. Naming a group turns off backend rule management. With `preserve_client_ip` on IP targets, the node and pod security groups must admit the client address, and `manage-backend-security-group-rules` keeps them in step.

The tunnel is a NodePort with `externalTrafficPolicy: Cluster`. A LoadBalancer would put the tunnel's bytes through a per-GB-processed NLB on top of the receiver's door, and a NodePort carries no per-byte charge. The deployer reaches it through an address they bring, such as an Elastic IP on a small forwarder in the receiver's AZ. A NodePort makes `loadBalancerSourceRanges` inert, so the allow list belongs on the node security group, and the chart rejects the pair. `Cluster` and not the chart's `Local` default, because a forwarder that does not track pod placement needs every node's NodePort to answer. The cost is the client's source IP, which `Cluster` SNATs to a node, so culvert's PKI and OIDC identity are the sole attribution a connection carries.

`peers.classes.admin.enabled: true` is the reach-back an operator administers one appliance through (`dfe-ops bastion hub`). An admin that dials in as a peer waits on hyperi-io/culvert#40.

The receiver's NLB (`apps/dfe-receiver/aws.yaml`) uses the same annotations. Its security group is the frontend group the allow list fills, so dfe-extras rejects a named `aws-load-balancer-security-groups`. On GKE and AKS the receiver's load balancer targets nodes with `externalTrafficPolicy: Local`, because `Cluster` would SNAT each sender to a node address before the rate limit and `ip_filter` see it.

