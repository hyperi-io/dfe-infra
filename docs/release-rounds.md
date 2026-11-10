# Release rounds

How a release candidate is proven, and how long each proof takes. The loop each proof runs is [testing-cycle.md](testing-cycle.md).

## Release rounds

We prove a release candidate in ROUNDS. A round is one candidate plus every proof run against it in parallel: the cloud-cycle legs ([backends](cloud-cycle.md#choosing-the-backend-for-one-run)), the k8s deploy, the dfe-docker VM, the dfe-ui deployed e2e and the `dfe-sweep` gating check. Nothing is fixed mid-round.

Each find is recorded as it lands. A find that blocks a proof doesn't stop the round when a workaround for that cycle alone lets the other proofs continue, such as a manual reload or a hand-applied config. The workaround goes into the round's findings and is never shipped.

After the round we fix the WHOLE accumulated set, blocking and non-blocking alike, as one batch. The fixes run in parallel and merge without release trailers. Then each touched repo cuts ONE release and the stack cuts ONE new candidate. A find too big or risky for that batch becomes an issue labelled `next-patch`, which `dfe-sweep` schedules without gating, and never re-opens the candidate.

The next round re-runs only the proofs the batch can affect. A batch touching several components re-runs the union of their rows.

| Changed | Re-run |
|---|---|
| dfe-ui only | dfe-ui deployed e2e, the k8s deploy |
| dfe-engine | the k8s deploy, the VM, the source-suite cloud legs |
| dfe-hyperdx | the k8s deploy, the VM, the source-suite cloud legs (the `observe` step reads it) |
| dfe-receiver, dfe-loader, the transforms | every cloud leg, the VM, the k8s deploy |
| dfe-infra charts or tofu | the cloud legs on the touched backend, the k8s deploy |
| pin bumps in versions.yaml only | `dfe-sweep`, the k8s deploy |

`msk` is in no row unless its re-test is explicitly approved ([MSK runs last, and rarely](cloud-cycle.md#msk-runs-last-and-rarely)).

A round ends early only for a find that blocks a proof and has no workaround, such as the deploy itself broken or an AWS deny on the account. That find may cut a candidate early, because nothing else in the round can run until it's fixed.

## How long each proof takes

A full cycle, build to test to tear down, takes a while:

| Target | Full cycle |
|---|---|
| AWS, default backends | about 2 hours |
| AWS with Amazon MSK | a lot longer |
| On-premises Rancher | not measured yet |

Your mileage will vary. The region, how busy the cloud is that day, CI queues and which backends you turn on all move it. Plan on the rough figure, not to the minute.

These are measured durations from the reference runs in October 2026, not estimates. Cloud times move with the region and the backend, so read them as a guide. Figures are minutes, medians with the range across runs, and `n` is the number of runs behind the figure.

Teardown is the long pole. Destroying the workloads, `tofu destroy` and the sweep of leftovers add up to a median 35 min, about a third of every cloud cycle. The KEDA scale proof is 13.6 of the 19.5 min smoke stage. The two Confluent Cloud runs took about 4 min longer to sweep than the Redpanda Cloud and ClickHouse Cloud runs. The k8s deploy figure is a fresh install, and the VM figures are the scenarios that passed.

| Proof | Step | Median | Range | n |
|---|---|---|---|---|
| Cloud cycle | Whole job | 110 | 103-128 | 8 |
| Cloud cycle | Runner and tool setup | 1.9 | 1.6-2.8 | 8 |
| Cloud cycle | Guard preflight | 9.8 | 5.9-14.7 | 8 |
| Cloud cycle | `tofu apply` (cluster and backends) | 14.7 | 13.4-16.5 | 8 |
| Cloud cycle | Deploy plus the 2 default E2E tests | 18.0 | 16.4-24.3 | 8 |
| Cloud cycle | of which bootstrap | 6.3 | 6.2-7.8 | 8 |
| Cloud cycle | of which readiness gate | 7.4 | 7.1-8.1 | 8 |
| Cloud cycle | of which vertical-integration test | 2.6 | 1.9-7.7 | 8 |
| Cloud cycle | Smoke suite | 19.5 | 18.4-21.9 | 8 |
| Cloud cycle | of which KEDA scale proof | 13.6 | 13.4-13.8 | 8 |
| Cloud cycle | Acceptance, `source` suite | 4.7 | 4.5-5.1 | 5 |
| Cloud cycle | Acceptance, `edge` suite | 5.6 | - | 1 |
| Cloud cycle | Acceptance, `flows` suite | 12.6 | - | 1 |
| Cloud cycle | Acceptance, `all` suites | 16.4 | - | 1 |
| Cloud cycle | Diagnostics bundle | 3.0 | 2.4-3.4 | 5 |
| Cloud cycle | Destroy, workloads | 17.4 | 17.2-17.6 | 8 |
| Cloud cycle | Destroy, `tofu destroy` | 9.5 | 8.0-11.4 | 8 |
| Cloud cycle | Sweep of leftovers | 8.5 | 7.9-13.4 | 8 |
| k8s deploy | Fresh install, deploy plus 2 E2E tests | 10.6 | - | 1 |
| VM | `make ci` | 2.6 | - | 1 |
| VM | E2E, receiver to loader over gRPC | 1.6 | - | 1 |
| VM | E2E, complete single-node stack | 3.0 | - | 1 |
| VM | E2E, rules and hunts | 1.7 | - | 1 |
| VM | E2E, search, rule and hunt | 2.2 | - | 1 |
| Stack cut | PR checks, first start to last finish | 20.3 | 15.8-21.1 | 3 |
| Stack cut | Stack Release workflow | 1.1 | 0.7-1.2 | 3 |
| App release | dfe-engine, merge to tagged release | 24.4 | - | 1 |
| App release | dfe-hyperdx, merge to tagged release | 33.9 | - | 1 |
| App release | dfe-ui, merge to tagged release | 24.9 | - | 1 |
