# Cloud cycles

Running the [validation cycle](testing-cycle.md) against a cloud account: batching, unattended runs, the edge stage and the managed backends.

## Batching a full cloud cycle

A full cloud cycle runs once per BATCH of changes to the managed-Kafka path, never once per finding. A cycle proves a batch. Stage every proof job before `tofu apply`, so no cluster time is spent authoring one. The managed cluster is the long pole, so it comes up first and goes down first, and its proofs run the moment it reports ACTIVE. Proofs needing only the Kubernetes side run during its waits. A broker-count change runs one direction only. Teardown starts the moment the last proof lands.

## Unattended cloud runs

Run an unattended cloud cycle with `dfe-ops cloud-cycle --tf-dir <root> --run-length 3h -- <cycle args>`. The cycle's mode is the dial's profile. The guard adds `--mode <profile>`, and before anything is created it refuses a `--mode` that names another profile and any argument `dfe-ops cycle`'s own parser rejects.

It creates nothing unless `dfe-ops cloud-preflight` passes:

- The account's guardrails are readable.
- The credential outlives the run plus its teardown.
- No earlier run is left: no run record, and no run-tagged resource, expired or live. A NAT gateway, VPC endpoint, instance, network interface, volume, subnet, security group, security group rule, route table, internet gateway, VPC, launch template or Elastic IP that EC2 reports deleted, terminated or not found doesn't count. Nor does a KMS key scheduled for deletion or an EKS pod identity association whose cluster is gone, and the check says how many such hits it dropped.
- Check (e) refuses a runner missing any command bootstrap.sh or destroy.sh runs, on the PATH the run's children get. `bootstrap/check_tools.py` is the one list of them, and bootstrap.sh runs it again before its first cluster call.

The run gets its own state key and `dfe-e2e`/`expires-at` tags (`scripts/cloud_run.py`), and every exit short of SIGKILL tears it down. `scripts/cloud_reaper.py reap` removes what an expired run left, skips the same already-deleted resources the tagging API goes on listing, and does nothing while `DFE_REAPER_AWS_ROLE` is unset. Flags, variables and refusals: `scripts/dfe_ops_cloud_guard.py` and `scripts/cloud_reaper.py`.

Preflight also refuses a session credential (`AWS_SESSION_TOKEN`) whose expiry it can't read. Every child of a run gets the dial's region as `AWS_REGION`, so a call that names no region never lands outside the run's. A runner outside the VPC hands the guard its own address as `DFE_RUN_ENDPOINT_CIDR` (one /32). The run then opens the Kubernetes API's public endpoint to that address alone, in place of the dial's endpoint, and fences the public gateway to that address plus the cluster's NAT addresses. The private endpoint stays on, and the allowance goes with the cluster at teardown.

The GitHub Actions workflows that dispatch a cycle and schedule the reaper aren't in this repository. Their logs carry the account's resource ids and domain, so they run from a private repository that checks this one out and holds the GitHub environments the runner and reaper roles trust by OIDC.

A provider's error body can quote a credential it had just created, and an Actions log keeps it. `dfe-ops redact` writes stdin to stdout a line at a time through the failure-evidence redactor, so the step pipes its output through it: `set -o pipefail; tofu apply ... 2>&1 | scripts/dfe-ops redact`. Without `pipefail` the step exits with the redactor's status, 0 whatever tofu exited with. The redactor ignores SIGINT, SIGTERM and SIGHUP and ends at end of input, so a teardown running after the runner cancels the step still has a pipe to write to. A private key block is masked across its lines, and a line past 1 MiB is cut and ends in `***TRUNCATED***`.

The run length is a deadline that apply and the cycle share. A cycle still running at it is stopped with its process tree and the run exits 124, leaving the whole margin (`--teardown-margin`, default 45m) to the teardown. The workload teardown gets half of it, so `tofu destroy` always runs. When that destroy fails, the teardown deletes the network interfaces carrying the run's tag and runs `tofu destroy` once more. The VPC CNI allocates them outside tofu, and one whose node terminated mid-destroy holds its subnet. Nothing else is swept while state remains, and an interface EC2 won't delete is named and left.

`dfe-ops cycle --acceptance-suite <suite>` runs `acceptance` as a stage after smoke and before ui, from the deploy's terraform outputs and as its minted admin. A failed stage skips the rest and destroy still runs.

The suite runs from the dfe-engine checkout `acceptance --repo` names, and that checkout must be the exact tag the stack under test pins (`apps.dfe-engine` in versions.yaml) on a clean tree. The same goes for the checkouts the source suite reads its corpus and program from (`dfe-transform-vrl`, and `dfe-transform-vector` for the vector case) and for the dfe-ui checkout the `ui` stage runs. Otherwise `acceptance`, `ui` and the cycle (before it deploys anything) refuse, naming both versions. `--dev` runs a local tree on purpose and prints a WARNING with both versions.

The source and edge suites leave nothing behind. Each reads the deployment before the run and again after its teardown: the receiver's ConfigMaps, every Deployment and its replicas (one an HPA scales is left out), the sources the engine lists, the receiver's lumberjack overlay and the harness namespaces. Any difference fails the run with a `no-trace-<part>` row, on a pass and on a failure alike. `--keep` leaves only its own source and the apps named after it. The edge suite leaves out the receiver ConfigMaps, which `dfe-ops` rolls with the allow list and restores after the run.

### The edge stage

`--acceptance-suite edge` proves the receiver's public door lets in the address on the allow list and no other. It admits one address on the ingest door and sends a record from it. The send has to be accepted, the row has to land in `dfe.main`, and both of the DFE console's search views have to find it. Then it sends from the cluster's own NAT egress, which the list doesn't name, and expects a refusal. That proves the list rather than a dead door.

The stage writes the allow list to the deploy repo's overlay and puts it back afterwards (`--keep` leaves it). Only a cloud lane has a public load balancer to send to, so `all` never includes it. It reads two inputs:

- `DFE_RUN_INGEST_ALLOW_CIDR` is the address the suite sends from, taken from the environment or an `--env-file`. It has to be one network and never `/0`, because a refusal can't prove a list that admits everyone.
- `DFE_EGRESS_CIDRS` is the cluster's NAT egress as /32s, taken from the cloud root's tofu output through `--from-terraform`.

A missing or `/0` allow address is refused before it costs a cluster. `dfe-ops cloud-cycle` refuses it ahead of the preflight and the apply, and `dfe-ops cycle` refuses it ahead of the deploy.

### Choosing the backend for one run

`scripts/cycle_backend.py --dial <file> --backend <name>` picks what a run deploys against without editing the one dial. `default` runs the dial as written. Any other name applies `scripts/cycle-backends/<name>.yaml` over it, so it has to run before the tofu render and the sizing read the dial. An overlay commits only the shape (`msk` sets `kafka.provider: msk` and the `kafka.msk` block), and a value written `env:<NAME>` is read from the environment, so no host or cluster id reaches the repository or its logs.

| Backend | What the run deploys against | Read by the overlay |
|---|---|---|
| `msk` | AWS MSK brokers the run creates | nothing |
| `confluent-cloud` | a Confluent Cloud Basic cluster per run, public endpoint, in a standing environment, topics capped at 8388608 bytes | `DFE_CYCLE_CONFLUENT_ENVIRONMENT_ID` |
| `redpanda-cloud` | a Redpanda Cloud Serverless cluster per run, public endpoint, in a standing resource group | `DFE_CYCLE_REDPANDA_RESOURCE_GROUP_ID` |
| `clickhouse-cloud` | the standing ClickHouse Cloud service, beside the dial's own broker | `DFE_CYCLE_CH_HOST`, `DFE_CYCLE_CH_USER` |

Every backend a run names is its AWS instance, and the plan refuses one on another cloud ([deployment/managed-backends.md](deployment/managed-backends.md)).

### MSK runs last, and rarely

An MSK cluster is the slowest thing a run builds: one took 41m14s to create on 2026-10-14, and the EKS cluster sat built and idle for about 27 minutes of that wait. Brokers bill by the broker-hour and the cluster, nodes and NAT gateways bill by the hour through all of it, for no test value. So iterate on `confluent-cloud` or `redpanda-cloud`, and run `msk` once, after the shared external-Kafka changes have already passed on one of them.

An MSK cluster can't be paused either, so it bills from create to delete whether a test is running or not. So MSK was tested once, as thoroughly as that run allowed, with 2.2.1. It is not re-tested unless that is explicitly approved: as a last resort, for a change that truly needs a real MSK cluster to prove, or alongside AWS if they want to be involved in that testing.
