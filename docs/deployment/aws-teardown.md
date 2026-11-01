<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      docs/deployment/aws-teardown.md
Purpose:   Operator guide for tearing a DFE cluster on AWS down -- lifecycle
           tags, the teardown order, and sweeping what is left.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# DFE on AWS -- Teardown

Provisioning: [aws.md](aws.md). Running the cluster:
[aws-operations.md](aws-operations.md).

## Lifecycle tags

- `tags.lifecycle: ephemeral` (tyre-kick, provision-test-destroy) is the one
  governance tag that changes what gets built, not only what gets labelled.
  It sets the secret recovery windows on both the deployment's own secrets
  and the Kafka SCRAM credential (0 days instead of 30), and redpanda-cloud's
  `allow_deletion` (true instead of the vendor's own default refusal) -- so a
  tyre-kick deployment tears down cleanly and anything else keeps the vendor's
  normal deletion protection, without a per-resource flag to remember. It also
  decides what a destroy keeps. An ephemeral deployment's KMS key is scheduled
  for deletion (7 days) and its ClickHouse object-store, CloudTrail and
  session-log buckets go with their objects. Any other deployment keeps the key
  and all three buckets, each with its policy, encryption and lifecycle, because
  tofu forgets them rather than deleting them. A bare `tofu destroy` then ends
  with `Destroy was successful but left behind forgotten instances` unless you
  pass `-suppress-forget-errors`. `dfe-ops teardown --with-terraform` passes it
  on a persistent deployment only, read from the root's `retained_on_destroy`
  output, and its last lines name every address it kept. Delete the buckets once
  nothing needs what they hold, then schedule the key's deletion. Every `dfe-ops`
  command against an ephemeral dial also opens with one stderr line: the
  resolved `compute_bucket` from `sizing/resolved.yaml` -- a T-shirt bucket, <!-- doc-paths: ignore -->
  never a rate -- plus how long the cluster has been up, from the root's
  `cluster_created_at` output. Nothing in the tree
  dates a deployment -- a local state file's mtime dates the last apply, and
  the S3 backend leaves none here -- so the control plane's own stamp is the
  reading, and a dial with no cluster behind it reports the age unavailable.
  Neither reading may fail the command.
- During a test run (`controller_tags` set) each managed node group launches through a launch template that puts the run's tags on its instances and root volumes, and every Karpenter pool is held to `pools.aws.runInstanceSizes`. Outside a run there is a template only when the registry cache writes a node script, because adding one to an existing node group replaces the group.

## Tearing down

- A full cloud cycle is batched, never run per finding
  ([testing-cycle.md](../testing-cycle.md)). Two halves bear on teardown: the
  managed cluster goes down first, being the long pole both ways, and teardown
  starts when the last proof lands, not at the end of a session.
- `bootstrap/destroy.sh` deletes the node pools' Application, `karpenter-pools-<cluster>` found by its name, once the load balancers are released and keeps Karpenter itself until every NodeClaim is gone (up to 600s), because a node left running holds its subnets through its network interfaces and `tofu destroy` fails on them. On a test run its last step terminates any node a Karpenter pool launched for that run and cluster.
- HyperDX's store keeps its volumes (reclaim policy `Retain`) while it runs. Once the DocumentDB operator has gone, `bootstrap/destroy.sh` sets the volumes labelled `documentdb.io/cluster=dfe-documentdb,documentdb.io/namespace=cnpg` to `Delete`, so the EBS CSI driver deletes each one with its disk. A `tofu destroy` run without it first leaves those disks behind.
- Tear down with `tofu destroy` in `terraform/environments/aws`, deleting
  the Kubernetes workloads first -- anything that made a load balancer, a
  volume or a DNS record did so through a controller, and `tofu destroy`
  does not know about it.
- **An untargeted `tofu destroy` on an MSK dial fails with `Client
  configuration missing` until the Redpanda credentials are in the
  environment.** The root declares `provider "redpanda"`, OpenTofu configures
  every provider the configuration requires whether or not the dial selects
  it, and that provider's client resolves `REDPANDA_CLIENT_ID` and
  `REDPANDA_CLIENT_SECRET` or fails. Until the root stops requiring it, either
  export those two with any value for the run, or destroy by target. Do NOT
  reach for `-exclude`: it cannot be combined with `-target`.

## Sweeping what is left

- Run `scripts/cloud_sweep.py --region
  <region>` read-only to see what is left, add `--exclude-bucket <state
  bucket>` so the state store is never a candidate, and only add `--delete`
  once you have read the listing. `--delete` requires `--account <12-digit>`,
  checked against `aws sts get-caller-identity` before anything is touched --
  refused on a mismatch or a missing `--account`, the same guard both tofu
  roots already carry against running against the wrong account. `--delete`
  also prints the full eligible list and asks for a typed `yes` before
  deleting anything, and `--yes` skips that prompt for a CI run. `--include-untagged`
  widens the sweep to every resource the listers found, tagged or not, turning
  `--delete` into a region-wide wipe of the account (`--exclude-bucket` is the
  one exemption). Without it, `--delete` only ever removes resources carrying
  the tag filter. The AWS
  account defaults -- the default VPC and everything that comes with it,
  AWS-managed KMS aliases -- are never listed or touched.
- `--expired` selects resources whose `dfe-e2e` run tag carries an `expires-at` past `--grace` (default 1h), plus any non-default security group with no run tag inside a VPC so selected -- EKS creates its cluster security group untagged and outside tofu state, and the VPC cannot be deleted while it stands. Unattended runs and their reaper: [testing-cycle.md](../testing-cycle.md).
