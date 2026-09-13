# kubernetes-cluster -- module contract

One managed Kubernetes cluster, its network, its DNS zones and the workload
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
| `node_pools` | `map(object(...))` | Keyed by pool name. Each pool: `shape_ref`, `min_size`, `max_size`, `desired_size`, `capacity_type`, `disk_gb`, `labels`, `taints`. |
| `resolved_shapes` | `map(object({ instance_types, arch }))` | Keyed by `shape_ref`. The resolver's answer, never an instance type written by hand. `instance_types` is ordered best-first so the cloud can fall back when the first is unavailable. |
| `network` | `object({ nat })` | `nat` is `per-az` (one NAT per availability zone, no cross-zone charge and no single point of failure) or `single` (one NAT for the whole network, cheaper). |
| `endpoint` | `object({ public, allowed_cidrs })` | The private API endpoint is always on. `public` adds the public one, restricted to `allowed_cidrs`. |
| `dns` | `object({ private_zone, public_zone })` | The private zone always exists. `public_zone` of `""` means no public zone and no public-DNS identity. |
| `telemetry` | `object({ sink, retention_days })` | Where a CloudWatch-only touchpoint lands. Every cloud's control plane delivers SOME logs to a CloudWatch-shaped sink with no other export path -- on `aws/` that is the EKS audit log, always on, always in CloudWatch. `sink = otel` (the default) pins its retention to a 1-day floor since it is an unavoidable exception, not a chosen destination; `sink = cloudwatch` keeps it at `retention_days` like every other touchpoint under that sink. |
| `tags` | `map(string)` | The six required governance tags plus `iac-source`. The body VALIDATES them; the root APPLIES them through the provider's default-tag mechanism, so a body never tags a resource with this map itself. |

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
| `network` | `object({ vpc_id, cidr, azs, private_subnet_ids, public_subnet_ids })` | What the managed-kafka module attaches to. |
| `private_zone_id` | `string` | |
| `public_zone_id` | `string` | `""` when there is no public zone. |
| `public_zone_name_servers` | `list(string)` | The delegation the parent zone needs. Empty when there is no public zone. |
| `kms_key_arn` | `string` | The deployment's own key. Encrypts cluster secrets today; Kafka and block storage take the same key. |
| `pod_identity_trust_policy_json` | `string` | The trust policy any further workload-identity role in this cluster assumes. Lets a sibling module mint a role without knowing how this cloud expresses cluster trust. |
| `node_role_arn` | `string` | The node instance role, for a caller that has to grant nodes something extra. |
| `karpenter` | `object({ interruption_queue, instance_profile, discovery_tag, controller_role_arn, node_role_arn })` | AWS-only, and the one output a body may omit: what the karpenter and karpenter-pools charts have to be told. A body whose cloud has no Karpenter emits its own provisioner's handles under its own name, or nothing. |
| `audit_log_group` | `string` | The CloudWatch log group the control-plane audit stream lands in -- unavoidable under either telemetry sink, so a caller reads this rather than reconstructing the naming convention. |

## Rules every body follows

- **ARM in the cloud.** A cloud body refuses a `shape_ref` whose resolved
  `arch` is not `arm64`. On-prem is x86_64 most of the time, so `rke2/` will
  not carry that rule.
- **Private by default.** Nodes, the data plane and every internal service sit
  in private subnets. Public subnets carry load balancers and egress only.
- **No provider block.** A body declares `required_providers` and nothing else;
  the root configures the provider.
- **Nothing hardcoded.** The only literals allowed are ones the cloud's own API
  defines: managed policy names, add-on names, the machine-image family,
  Kubernetes label keys and service principals. Each carries a one-line comment
  saying why it is structural.
