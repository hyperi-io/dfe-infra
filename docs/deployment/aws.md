<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      docs/deployment/aws.md
Purpose:   Deploy DFE on AWS: EKS on Graviton with managed Kafka, from the
           dial to a tested stack, how costs are described, receiver ingress.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# DFE on AWS

DFE on Amazon EKS in your own AWS account. OpenTofu builds the network, the cluster on Graviton, the managed Kafka and the stores. Then the same bootstrap as every other cluster deploys DFE onto it, and sends an event through to ClickHouse to check it.

Kafka comes from Amazon MSK, Confluent Cloud or Redpanda Cloud, or runs in the cluster on Strimzi. ClickHouse runs in the cluster, or you bring ClickHouse Cloud. Every managed backend is the vendor's AWS instance ([managed-backends.md](managed-backends.md)). One file drives the lot: the deployment dial, `deployment.yaml`.

Every dial field, sizing lock and backend detail is in [aws-configuration.md](aws-configuration.md). Running it is [aws-operations.md](aws-operations.md), and taking it down [aws-teardown.md](aws-teardown.md).

## Deploy

You need:

- OpenTofu 1.12 or later, the `aws` CLI, `kubectl`, `helm`, `openssl` and Python 3.
- A shell signed in to the target account. `aws sts get-caller-identity` must print the account the dial names, and the plan refuses if it does not.
- The state bucket, once per account. In `terraform/environments/aws-state`, copy `example.tfvars.json` to `state.auto.tfvars.json`, set the account, region, name, env, `bucket_prefix` and tags, then `tofu init && tofu apply`. Its `state_bucket` output goes in the dial's `state.bucket`, beside a `state.key` no other deployment shares.
- A public DNS zone you control, if you want public hostnames. The apply creates the Route 53 zone, and delegating to it from the parent is yours to do.

`<version>` is the DFE release you are installing.

1. Clone the release and run the wizard.

   ```bash
   git clone --branch <version> https://github.com/hyperi-io/dfe-infra && cd dfe-infra
   python3 scripts/dfe-ops init
   ```

   `dfe-ops init` asks nine questions and writes `deployment.yaml` ([wizard.md](wizard.md)). Answer `aws` for the target, `scale` for the tier, and `msk`, `confluent-cloud` or `redpanda-cloud` for Kafka.

2. Size it.

   ```bash
   python3 scripts/resolve_sizing.py --dial deployment.yaml --live \
     --out terraform/environments/aws
   ```

   The resolver asks the AWS API what the region offers and sizes the node pools from your GB/day estimate. Leave the estimate blank for the smallest shape that runs. It writes `sizing.auto.tfvars.json` into the OpenTofu root and the rest under its `sizing/`. Commit that directory to your deploy repo as the baseline a re-size checks against.

3. Render the OpenTofu inputs. This writes `dial.auto.tfvars.json` beside the sizing file.

   ```bash
   python3 scripts/render_dial.py --tofu
   ```

4. Export the Kafka vendor's credentials. They never go in a file. Export `CONFLUENT_CLOUD_API_KEY` and `CONFLUENT_CLOUD_API_SECRET` for Confluent Cloud. The Redpanda provider checks for `REDPANDA_CLIENT_ID` and `REDPANDA_CLIENT_SECRET` on every plan, whichever Kafka you chose.

5. Build the cluster. `tofu init` reads the state bucket, key and region from the tfvars.

   ```bash
   (cd terraform/environments/aws && tofu init \
     && tofu plan -out=deployment.tfplan && tofu apply deployment.tfplan)
   ```

6. Deploy DFE onto it.

   ```bash
   eval "$(tofu -chdir=terraform/environments/aws output -raw kubeconfig_command)"
   python3 scripts/dfe-ops stack-deploy --stack <version> --mode scale \
     --from-terraform terraform/environments/aws
   ```

`--check-only` on `stack-deploy` runs its offline checks alone, with no cluster contact.

The apply builds the VPC, EKS and its node groups, the managed Kafka when the dial names one, the KMS key, the Route 53 zones and the Secrets Manager store. A managed broker builds beside the cluster and takes longest, so it sets when the apply ends. OpenTofu writes no outputs until then, so the deploy waits on it too.

`stack-deploy` reads the outputs, checks the pins and renders, then bootstraps. Argo CD syncs the backing services and the apps. A readiness gate and two data checks follow: an event posted to the receiver lands in ClickHouse, and the stack's own logs and metrics land too.

The run ends on `=== stack-deploy OK ===` and the path of `.tmp/<version>-scale/access-summary.md`, at mode 0600. It holds the console URL, the admin and break-glass passwords and what to do next.

## Tear down

Workloads first, then the OpenTofu state. Load balancers, volumes and DNS records were made through controllers, and `tofu destroy` does not know about them.

```bash
python3 scripts/dfe-ops teardown --with-terraform --cloud aws
```

Run it where the `aws` CLI is installed, because `tofu destroy` calls it. It runs `bootstrap/destroy.sh`, which asks you to type `yes`. Then `tofu destroy` in `terraform/environments/aws`, then it deletes the secrets dfe-engine wrote to Secrets Manager outside the state. The state bucket is `prevent_destroy`, so removing it is a separate act. [aws-teardown.md](aws-teardown.md) has the order, the lifecycle tags and the sweep for anything left.

## Troubleshooting

| What you see | What to do |
|---|---|
| The plan refuses the account | Your shell is signed in to another account. Check `aws sts get-caller-identity`. |
| The plan refuses a public API endpoint | `endpoint.public: true` needs `endpoint.allowed_cidrs`. |
| Public names do not resolve, cert-manager's DNS-01 challenges fail | The parent zone does not delegate yet. Point it at the NS set `tofu output public_zone_name_servers` prints. |
| Every Layer 2 Application `Unknown`, "authentication required" | Your copy of the chart repo (`k8s.repo_url`) is private. Export `DFE_REPO_TOKEN`, an HTTPS token with read access. |
| `stack-deploy` refuses `openbao` | AWS keeps every secret in Secrets Manager. Unset `DFE_SECRETS_BACKEND`, or set it to `aws-sm`. |
| Bootstrap refuses with no `<tier>.pools.json` | The resolve wrote somewhere else. Set `DFE_SIZING_DIR` to its `sizing/` directory. |
| The resolver exits 3 | A re-resolve moved a locked field ([sizing locks](aws-configuration.md#sizing-dials-and-locks)). Pass `--migrate` only if you mean it. |
| `tofu destroy` fails with `Client configuration missing` | Export the two Redpanda variables, any value, for the run ([aws-teardown.md](aws-teardown.md#tearing-down)). |

## How costs are described

Costs here are T-shirt buckets, never figures. A bucket is relative to the deployment's own compute, never to a currency: XS is negligible beside the cluster's compute, S a small fraction of it, M of the same order as one dedicated node, L of the same order as the whole cluster's compute, and XL larger than the cluster's compute.

A pricing model is named wherever it decides whether an option is safe -- hourly, per GB processed, per request, per LCU-hour, per broker-hour -- but never a rate. Rates move by region, by account and by discount, so price your own account rather than trust a number written down here. Every edge door is priced on this scale in [edge.md](edge.md).

## Receiver ingress

DFE pushes terabytes a day through the receiver, and every AWS front door bills by volume: a Network Load Balancer charges per LCU-hour (1 GB processed), a Classic ELB per GB, CloudFront and Global Accelerator per GB, WAF per request. The receiver's door is its own NLB, the cheapest of those per GB, and no protection with a volume or request charge sits on it.

A fresh deploy renders no NLB at all, and the receiver stays on a ClusterIP. The default is `exposure.groups.ingest.door: loadbalancer` with an empty `allow` list. Name the senders in the deploy repo's `exposure.groups.ingest.allow` and the NLB renders: AWS Load Balancer Controller, IP targets, client address preserved, its frontend security group holding the list. It sits on the Elastic IPs the AWS root allocates, one per public subnet.

An NLB bills an hourly charge plus a per-LCU-hour charge, one LCU-hour being 1 GB processed for TCP/UDP (verified 2026-09-15 against AWS's Elastic Load Balancing pricing page). The hourly half is XS. The processing half is the transport cost of ingest on this door. AWS data transfer out still applies to the replies on every path, culvert included.

Those Elastic IPs exist, and AWS bills them, while the ingest door is closed and no NLB exists. `edge.aws.receiver_eips: false` turns them off. [ingest-edge.md](../ingest-edge.md) describes the doors, and [edge.md](edge.md) carries the AWS tier table: every door, its group, its tier, the dial key that turns it and its cost bucket.
