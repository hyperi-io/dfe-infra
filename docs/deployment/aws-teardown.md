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

Provisioning is in [aws.md](aws.md). Running the cluster is in [aws-operations.md](aws-operations.md).

## Lifecycle tags

- `tags.lifecycle: ephemeral` (tyre-kick, provision-test-destroy) is the one governance tag that changes what gets built and not just what gets labelled. It sets the secret recovery windows on the deployment's own secrets and the Kafka SCRAM credential to 0 days instead of 30. It sets redpanda-cloud's `allow_deletion` to true instead of the vendor's own default refusal. So a tyre-kick deployment tears down cleanly, anything else keeps the vendor's normal deletion protection, and nobody has to remember a per-resource flag.
- The tag also decides what a destroy keeps. An ephemeral deployment's KMS key is scheduled for deletion (7 days) and its ClickHouse object-store, CloudTrail and session-log buckets go with their objects. Any other deployment keeps the key and all three buckets, each with its policy, encryption and lifecycle, because tofu forgets them and doesn't delete them. A bare `tofu destroy` then ends with `Destroy was successful but left behind forgotten instances` unless you pass `-suppress-forget-errors`. `dfe-ops teardown --with-terraform` passes it on a persistent deployment only, read from the root's `retained_on_destroy` output, and its last lines name every address it kept. Delete the buckets once nothing needs what they hold, then schedule the key's deletion.
- Every `dfe-ops` command against an ephemeral dial opens with one stderr line. It gives the resolved `compute_bucket` from `sizing/resolved.yaml` (a T-shirt bucket, never a rate) and how long the cluster has been up. <!-- doc-paths: ignore --> Deployment age comes from the root's `cluster_created_at` output. A dial with no cluster behind it reports the age as unavailable. Neither reading may fail the command.
- During a test run (`controller_tags` set) each managed node group launches through a launch template that puts the run's tags on its instances and root volumes, and every Karpenter pool is held to `pools.aws.runInstanceSizes`. Outside a run there's a template only when the registry cache writes a node script, because adding one to an existing node group replaces the group.

## Tearing down

- Start teardown as soon as your last check passes. The managed Kafka cluster takes longest to create and to delete, so it goes down first. When you're testing, batch your checks into one full cloud cycle and don't run one per finding ([cloud-cycle.md](../cloud-cycle.md#batching-a-full-cloud-cycle)).
- `bootstrap/destroy.sh` deletes the node pools' Application, `karpenter-pools-<cluster>` found by its name, once the load balancers are released. It keeps Karpenter itself until every NodeClaim is gone (up to 600s), because a node left running holds its subnets through its network interfaces and `tofu destroy` fails on them. On a test run its last step terminates any node a Karpenter pool launched for that run and cluster.
- The dfe-hyperdx store keeps its volumes (reclaim policy `Retain`) while it runs. Once the DocumentDB operator has gone, `bootstrap/destroy.sh` sets the volumes labelled `documentdb.io/cluster=dfe-documentdb,documentdb.io/namespace=cnpg` to `Delete`, so the EBS CSI driver deletes each one with its disk. A `tofu destroy` run without it first leaves those disks behind.
- Tear down with `tofu destroy` in `terraform/environments/aws`, and delete the Kubernetes workloads first. Anything that made a load balancer, a volume or a DNS record did so through a controller, and `tofu destroy` doesn't know about it.
- An untargeted `tofu destroy` on an MSK dial fails with `Client configuration missing` until the Redpanda credentials are in the environment. The root declares `provider "redpanda"`, and OpenTofu configures every provider the configuration requires whether or not the dial selects it. That provider's client resolves `REDPANDA_CLIENT_ID` and `REDPANDA_CLIENT_SECRET` or fails. Until the root stops requiring it, export those two with any value for the run, or destroy by target. Don't reach for `-exclude`, because it can't be combined with `-target`.

## Rebuilding a persistent deployment under the same name

A persistent destroy leaves the key and the three buckets in the account, so an apply under the same dial name fails creating them (`BucketAlreadyOwnedByYou`). Import them first, from `terraform/environments/aws`, so the new deployment takes over the kept data and doesn't start beside it. The key and each bucket are in the `kept` lines the teardown printed. The key's ID is its ARN after `key/`, and a bucket's name is its ARN after `arn:aws:s3:::`.

| Address | Import ID |
|---|---|
| `module.cluster.aws_kms_key.retained[0]` | the key's ID |
| `module.cluster.module.clickhouse_object_store.<resource>.retained[0]` | the ClickHouse bucket's name |
| `module.cloudtrail_bucket[0].<resource>.retained[0]` | the CloudTrail bucket's name (only with `cloudtrail.enabled`) |
| `module.toolbox_session_logs.<resource>.retained[0]` | the session-log bucket's name |

`<resource>` is each of `aws_s3_bucket`, `aws_s3_bucket_public_access_block`, `aws_s3_bucket_ownership_controls`, `aws_s3_bucket_policy`, `aws_s3_bucket_server_side_encryption_configuration` and `aws_s3_bucket_lifecycle_configuration`, all imported by the bucket's name, plus `aws_s3_bucket_versioning` on the CloudTrail and session-log buckets. For example:

```bash
tofu import 'module.cluster.aws_kms_key.retained[0]' 00000000-0000-0000-0000-000000000000
tofu import 'module.toolbox_session_logs.aws_s3_bucket.retained[0]' dfe-toolbox-session-logs
```

Then `tofu plan` shows the imported resources unchanged and everything else to create. The key's alias is recreated, pointing at the imported key.

## Sweeping what is left

- Run `scripts/cloud_sweep.py --region <region>` read-only to see what's left. Add `--exclude-bucket <state bucket>` so the state store is never a candidate. Only add `--delete` once you've read the listing.
- `--delete` requires `--account <12-digit>`, checked against `aws sts get-caller-identity` before anything is touched. It refuses on a mismatch or a missing `--account`, the same guard both tofu roots already carry against running in the wrong account. It also prints the full eligible list and asks for a typed `yes` before deleting anything. `--yes` skips that prompt for a CI run.
- `--include-untagged` widens the sweep to every resource the listers found, tagged or not, which turns `--delete` into a region-wide wipe of the account (`--exclude-bucket` is the one exemption). Without it, `--delete` only removes resources carrying the tag filter. The AWS account defaults are never listed or touched: the default VPC and everything that comes with it, and AWS-managed KMS aliases.
- `--expired` selects resources whose `dfe-e2e` run tag carries an `expires-at` past `--grace` (default 1h), plus any non-default security group with no run tag inside a VPC so selected. EKS creates its cluster security group untagged and outside tofu state, and the VPC can't be deleted while it stands. Unattended runs and their reaper are in [cloud-cycle.md](../cloud-cycle.md#unattended-cloud-runs).
