# kubernetes-cluster -- module contract

REFERENCE. One managed Kubernetes cluster, its network, its DNS zones and the workload
identities the in-cluster controllers need. One body per implementation:

| Body | Status | Cluster | Workload identity |
|------|--------|---------|-------------------|
| `aws/` | built | EKS | EKS Pod Identity |
| `gcp/` | not built | GKE | Workload Identity Federation |
| `azure/` | not built | AKS | Entra Workload ID |
| `rke2/` | not built | RKE2 on supplied nodes | none -- the secrets module carries it |

Every body takes the inputs below, produces the outputs below, and passes
`tests/contract.tftest.hcl` unchanged. A body that needs an input the contract
does not name is a contract change, not a body detail: add it here first, then
to every body, then to the test.

The caller is a per-cloud root under `terraform/environments/<cloud>/`. The root
owns the provider, the backend and the tags; the body owns the resources.

## Inputs

| Input | Type | Meaning |
|-------|------|---------|
| `provision` | `object({ account, region, cidr })` | Where the cluster goes. `account` is one free string whose meaning the body decides -- an AWS account id, a GCP project, an Azure subscription. `cidr` is the whole network the body subdivides. |
| `name` | `string` | Name prefix for every resource the body creates. |
| `env` | `string` | Deployment environment, for names a body has to disambiguate. |
| `kubernetes_version` | `string` | Control-plane minor version, e.g. `1.36`. |
| `addon_versions` | `map(string)` | AWS-only today. The managed add-on versions, keyed by add-on name, one for every add-on the body installs: pinned in tfvars, rendered from versions.yaml's `eks-addons`. Each is built for one Kubernetes minor, so a cluster on another minor sets every entry, and the plan refuses a kube-proxy built for any minor but `kubernetes_version`. |
| `node_pools` | `map(object(...))` | Keyed by pool name. Each pool: `shape_ref`, `min_size`, `max_size`, `desired_size`, `capacity_type`, `disk_gb`, `labels`, `taints`. |
| `resolved_shapes` | `map(object({ instance_types, arch, root_volume, instance_store_policy }))` | Keyed by `shape_ref`. Auto-selected by the shape resolver under each shape's generation policy: the newest arm64 generation in the region, or the generation the shape pins, with the reason and unpin trigger beside the pin in `shapes/compute-shapes.yaml`. Never an instance type written by hand. `instance_types` is ordered best-first so the cloud can fall back when the first is unavailable, holds at most the two newest generations, and a pinned shape can carry a single type. `root_volume` is the shape's root disk `type` and `size_gib`, and every node pool's shape carries one. `instance_store_policy` is `RAID0` on a shape whose volume profile asks for its instance store, and unset otherwise. On `aws/` a node group of that shape assembles its NVMe under kubelet and containerd through a nodeadm NodeConfig, the same array its Karpenter pool's `instanceStorePolicy` builds, and gains a launch template to carry it. |
| `node_image` | `object({ family, version })` | The image every node pool launches, through the node-image cascade below. `family` is the cloud's own image name -- an EKS AMI type, a GKE image type, an AKS OS SKU. `version` is the dated release where the body can pin one (`aws/`: the EKS AMI `release_version`, which the aws root requires), and null takes the newest the cloud offers when a pool is created. A body refuses a family that does not match its arm64 shapes. |
| `network` | `object({ nat, az_count, subnet_newbits })` | From tfvars. `nat` is `per-az` (one NAT per availability zone, no cross-zone charge and no single point of failure) or `single` (one NAT for the whole network, cheaper). `az_count` is how many availability zones the VPC spans, 2-6. `subnet_newbits` is how many bits each subnet adds to `provision.cidr`, which sets the addresses each zone's nodes and pods draw from. |
| `endpoint` | `object({ public, allowed_cidrs })` | The private API endpoint is always on. `public` adds the public one, restricted to `allowed_cidrs`. |
| `dns` | `object({ private_zone })` | The private zone always exists. No public zone here -- it and the identities that write it are the edge module's. |
| `telemetry` | `object({ sink, retention_days, forced_retention_days })` | From tfvars. Where a CloudWatch-only touchpoint lands. Every cloud's control plane delivers SOME logs to a CloudWatch-shaped sink with no other export path -- on `aws/` that is the EKS audit log, always on, always in CloudWatch. `sink = otel` keeps it at `forced_retention_days` since it is an unavoidable exception, not a chosen destination; `sink = cloudwatch` keeps it at `retention_days` like every other touchpoint under that sink. |
| `tags` | `map(string)` | The six required governance tags plus `iac-source`. The body VALIDATES them; the root APPLIES them through the provider's default-tag mechanism, so a body never tags a resource with this map itself. |
| `node_user_data` | `string`, default `""` | A shell script every node the body launches runs before kubelet starts: the registry-cache module's containerd mirror script. Empty adds none. On `aws/` it rides a launch template, and a managed node group that gains its first template is replaced. |
| `key_policy_grants` | `list(object({ sid, principals, actions, conditions }))`, default `[]` | AWS-only today. Extra KMS key policy statements for a service principal (`cloudtrail.amazonaws.com`, `delivery.logs.amazonaws.com`), which cannot be granted through an IAM role policy. This body is the key's ONE policy owner (`aws_kms_key_policy` replaces the whole policy), so a sibling module hands its statement in here rather than writing a second `aws_kms_key_policy` against the same key, which would silently replace this one's. The root computes the list. |

`node_pools` in full:

```hcl
map(object({
  shape_ref     = string
  min_size      = number
  max_size      = number
  desired_size  = number
  capacity_type = string                 # ON_DEMAND | SPOT
  disk_gb       = number
  labels        = map(string)
  taints        = list(object({ key = string, value = string, effect = string }))
}))
```

## Outputs

| Output | Type | Meaning |
|--------|------|---------|
| `cluster_name` | `string` | |
| `cluster_endpoint` | `string` | Kubernetes API server URL. |
| `cluster_ca` | `string`, sensitive | Base64 cluster CA for a kubeconfig. |
| `cluster_version` | `string` | The version the cloud actually runs, which can lead the requested one. |
| `oidc_issuer` | `string` | The cluster's OIDC issuer URL. |
| `cluster_security_group_id` | `string` | The group that admits a caller to the Kubernetes API. Nodes and pods are trusted already; anything else in the VPC has to be admitted by name. |
| `network` | `object({ vpc_id, cidr, azs, private_subnet_ids, public_subnet_ids })` | What the managed-kafka module attaches to. |
| `service_cidr` | `string` | The range Service addresses come from, as the cloud reports it. network-policies carves it out of every internet allow. |
| `nat_public_ips` | `list(string)` | The public addresses the cluster's own egress leaves from, one per NAT gateway. A fence on a public load balancer that in-cluster callers also reach has to admit them. |
| `private_zone_id` | `string` | |
| `private_zone_arn` | `string` | The private zone as an IAM resource, so the edge module's external-dns role is granted the internal half without creating the zone. |
| `kms_key_arn` | `string` | The deployment's own key. Encrypts cluster secrets today; Kafka and block storage take the same key. |
| `pod_identity_trust_policy_json` | `string` | The trust policy any further workload-identity role in this cluster assumes. Lets a sibling module mint a role without knowing how this cloud expresses cluster trust. |
| `node_role_arn` | `string` | The node instance role, for a caller that has to grant nodes something extra. |
| `karpenter` | `object({ interruption_queue, instance_profile, discovery_tag, controller_role_arn, node_role_arn })` | AWS-only, and the one output a body may omit: what the karpenter and node-pools charts have to be told. A body whose cloud has no Karpenter emits its own provisioner's handles under its own name, or nothing. |
| `audit_log_group` | `string` | The CloudWatch log group the control-plane audit stream lands in -- unavoidable under either telemetry sink, so a caller reads this rather than reconstructing the naming convention. |

## Rules every body follows

- **ARM in the cloud.** A cloud body refuses a `shape_ref` whose resolved
  `arch` is not `arm64`. On-prem is x86_64 most of the time, so `rke2/` will
  not carry that rule.
- **Private by default.** Nodes, the data plane and every internal service sit
  in private subnets. Public subnets carry load balancers and egress only.
- **No provider block.** A body declares `required_providers` and nothing else;
  the root configures the provider.
- **Nothing hardcoded.** An instance type, a size, a volume type, a count, a retention, a CIDR, a version or a node image arrives as an input with no default: an instance type and a root volume from the shape resolver, a node image from the node-image cascade below, everything else from tfvars. The only literals allowed are ones the cloud's own API defines: managed policy names, add-on names, Kubernetes label keys and service principals. Each carries a one-line comment saying why it is structural. The rule is stated once in `terraform/README.md`.

## Node images

A node image is a cascade, highest first, and OpenTofu orders the layers itself:

1. The provider's image, read live: `scripts/resolve_sizing.py images` writes it to `node-images.auto.tfvars.json` in the cloud's root, which loads after `terraform.tfvars`. The lookups and the release gates live in `scripts/node_images.py`.
2. A value in `terraform.tfvars`, which applies when the lookup failed or when the dial sets `node_image.pin: "true"` to skip it and empty the file -- the hold for a bad provider image or a reproducible rebuild. The pin takes effect on the next `images` run, because tofu never reads the dial.
3. The root's variable default, on a cloud whose image carries no version. The aws root has none: a node group with no release takes EKS's newest ungated, so a plan neither layer answers is refused by name.

The lookup runs in the resolver, never in a tofu data source, because a failing data source fails the whole plan and layer 2 would never get its turn. A failed lookup writes no key and warns once. The check refuses a node image literal, an image SSM path or an AMI data source anywhere in a `.tf` file.

Worker-node images are the one version class that moves without a DFE release, so on `aws/` every image clears release gates first. The knobs are `versions.yaml`'s `node-images` block, and `scripts/node_images.py` lists the gates and which image each applies to. The newest candidate that clears every gate wins. When none does, the value the deploy repo's `sizing/node-images.json` records holds, unless it is now skip-listed or below the floor.

Long-lived nodes take a DATED release, so their image moves only on the next resolve and apply.

| Body | Layer 1 reads | Writes | Layer 3 | Status |
|------|---------------|--------|---------|--------|
| `aws/` | awslabs/amazon-eks-ami's GitHub releases and open issues, then the SSM parameter `/aws/service/eks/optimized-ami/<kubernetes_version>/amazon-linux-2023/arm64/standard/<image name>/image_id` for the region ([EKS](https://docs.aws.amazon.com/eks/latest/userguide/retrieve-ami-id.html)) | `node_image = { family = "AL2023_ARM_64_STANDARD", version = "<patch>-<YYYYMMDD>" }` | None, refused by name | Implemented and tested, on recorded GitHub and SSM answers and in tofu tests. Cloud-cycle wiring pending, so no deploy has taken its answer yet |
| `gcp/` | `gcloud container get-server-config --location <region>`, its `defaultImageType`, once `validNodeVersions` carries the minor ([ServerConfig](https://docs.cloud.google.com/kubernetes-engine/docs/reference/rest/v1/ServerConfig)) | `node_image = { family = "COS_CONTAINERD" }` | `COS_CONTAINERD`, GKE's recommended image | Lookup coded and unit-tested against the documented response shape. No body, not deployed or tested live |
| `azure/` | `az aks get-versions --location <region>`, confirming the minor is listed and not preview ([AKS](https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/list-kubernetes-versions)) | `node_image = { family = "Ubuntu" }` | `Ubuntu` | Lookup coded and unit-tested against the documented response shape. No body, not deployed or tested live |
| `rke2/` | Nothing: no provider publishes a recommended node OS ([RKE2 requirements](https://docs.rke2.io/install/requirements), 2026-10-07, validates a list of distributions and recommends none) | Nothing | Its root's default | Layers 2 and 3 only. Not built |

GKE's `defaultImageType` is a machine answer. AKS publishes no API answer for an OS SKU: its guidance is the unversioned `Ubuntu` SKU, which moves to the newest validated Ubuntu with each minor ([OS versions](https://learn.microsoft.com/en-us/azure/aks/upgrade-os-version)), so the lookup writes it only once the location lists the minor as generally available.

On `aws/` the toolbox instance takes the root's `instance_ami_id` and the tunnel forwarder its `forwarder_ami_id`, both from the same command, the newest gated AMI in the family of `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64`. They are separate keys because `dfe-ops bastion up` refreshes the toolbox's alone, and a new AMI replaces the forwarder instance. Neither has a layer 3, because an AMI id belongs to one region: an unresolved one is refused by name, and only once its instance is on.

Karpenter's node class takes the same gated release as its alias, `al2023@v<YYYYMMDD>`, written to `sizing/node-pools.values.yaml`, which the deploy repo commits and layer2-platform reads under the deployer's own `infra/node-pools.yaml`. With none recorded, the chart default `operators.karpenter-al2023-ami` applies, and `scripts/check_karpenter_ami.py` warns in CI when it lags.
