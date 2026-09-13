# AWS deployment root

Creates the EKS cluster, its network, its Route 53 zones, the deployment's KMS
key and the Secrets Manager store the stack reads from. It creates no Kubernetes
workload -- `bootstrap/bootstrap.sh` does that, fed from this root's `DFE_*`
outputs.

Everything is config. There is no value in any `.tf` file that names a
particular deployment: account, region, network, cluster size, node shapes, DNS
zones and the state location all arrive in a tfvars file.

## What you need first

- OpenTofu 1.10 or newer (`tofu version`).
- A shell authenticated to the target AWS account, with permission to create
  VPC, EKS, IAM, KMS, Route 53 and Secrets Manager resources. Check with
  `aws sts get-caller-identity` -- the account it prints must match
  `provision.account`, and the plan refuses if it does not.
- A public DNS zone you control, IF you want public hostnames. This root creates
  the Route 53 zone; delegating to it from the parent is yours to do.

## Once per account: the state bucket

The state bucket has to exist before this root can initialise, so it gets its
own root with local state.

```bash
cd ../aws-state
cp example.tfvars.json state.auto.tfvars.json   # edit: account, region, name, env, bucket_prefix, tags
tofu init
tofu apply
```

`bucket_prefix` has no default. S3 bucket names are globally unique, so pick
your own organisation's prefix -- if you take a name someone else wants, they
lose it permanently, and if they took yours first, you do.

Note the `state_bucket` output.

## Then, per deployment

Render the dial. `deployment.yaml` at the repo root is the one file a deployment
turns, and the renderer writes this root's `dial.auto.tfvars.json` from it:

```bash
python3 ../../../scripts/render_dial.py --tofu
```

`example.tfvars.json` is the same shape written out by hand, for a deployment
that is not driven from a dial:

```bash
cp example.tfvars.json my-deployment.auto.tfvars.json
```

Keep only one. Every `*.auto.tfvars.json` in this directory is loaded, in name
order, and the later file overrides the earlier one with no warning.

Fill in, at minimum:

| Key | What it is |
|-----|------------|
| `provision.account` | The 12-digit AWS account id. Asserted against your shell before anything is created. |
| `provision.region` | Where it goes. |
| `provision.cidr` | The VPC network. Must not overlap anything you peer with, and must not be inside `172.31.0.0/16` -- that is the default VPC every AWS account already has. |
| `name` | Prefix for every resource. |
| `endpoint.allowed_cidrs` | The addresses allowed to reach the Kubernetes API. `curl -sS https://checkip.amazonaws.com` gives you yours. |
| `dns.private_zone` | Any name; it resolves inside the VPC only. |
| `dns.public_zone` | The delegated public zone, or `""` for none. |
| `state.bucket` / `state.region` | From the `aws-state` output above. |
| `state.key` | A path unique to this deployment. Two deployments sharing one key share one state, and the second destroys the first. |
| `tags` | All seven keys. The plan refuses on a missing one. |
| `kafka.provider` | `msk` creates the managed broker below; `confluent-cloud` and `redpanda-cloud` create a SaaS broker (see Kafka, below); `strimzi` or `redpanda` run in the cluster and this root creates nothing for them. |
| `kafka.msk.broker_version` | Defaulted nowhere. `aws kafka list-kafka-versions --region <region>` prints the ACTIVE ones -- take the newest, in MSK's `N.N.x.kraft` spelling. |
| `secrets.ref` | Optional. Empty puts the deployment's secrets at `<project>/<env>`, which is what a store holding one deployment wants. |

Then:

```bash
tofu init
tofu plan  -out=deployment.tfplan
tofu apply deployment.tfplan
```

`tofu init` reads `state.bucket`, `state.key` and `state.region` out of the
tfvars file, so there is no `-backend-config` to keep in step with it.

## After the apply

```bash
eval "$(tofu output -raw kubeconfig_command)"
kubectl get nodes
```

If you asked for a public zone, `tofu output public_zone_name_servers` gives the
NS set. Public names resolve only once the parent zone delegates to them, and
cert-manager's DNS-01 challenges fail until it does.

Then run the bootstrap, which reads the `DFE_*` outputs from here:

```bash
cd ../../..
python3 bootstrap/bridge.py --tf-dir terraform/environments/aws
```

## Nodes are arm64

Cloud node pools run Graviton. `resolved_shapes` carries the instance types and
the plan refuses a shape whose `arch` is not `arm64`, because the machine image
this root launches is arm64-only. Every image in the stack ships a multi-arch
manifest, so nothing else changes.

## Kafka

`kafka.provider: msk` adds an MSK cluster on the private subnets, its SCRAM
credential, and the IAM role the in-cluster bootstrap Job runs as.
`confluent-cloud` and `redpanda-cloud` add the matching SaaS body instead --
both size and tune the cluster themselves
(`terraform/modules/managed-kafka/CONTRACT.md`), so this root passes them only
`name`, `env` and `network`. `strimzi` and `redpanda` leave this root building
no broker at all.

The SCRAM password is generated here, once, and written to two places: the
secret MSK or redpanda-cloud authenticates against, and the deployment's own
store entry (`kafka/<provider>` under `DFE_SECRETS_PREFIX`) ESO projects into
the cluster. Never rotated by a re-apply -- the brokers keep the password they
were created with. Confluent Cloud has no SCRAM mechanism on any tier -- SASL
PLAIN over TLS with an API key as the username -- so no password reaches it;
the store entry carries the API key's ID instead of a generated secret.

Neither vendor's API credential is a tfvars field. Both providers read theirs
from the deployer's own shell, the same way an AWS credential already has to
be, via `versions.tf`'s deliberately empty `provider "confluent" {}` /
`provider "redpanda" {}` blocks:

| Provider | Environment variables | Authenticates |
|----------|------------------------|-------------------------|
| `confluent` | `CONFLUENT_CLOUD_API_KEY`, `CONFLUENT_CLOUD_API_SECRET` | The org-level key that creates the environment, cluster and private attachment. |
| `redpanda` | `REDPANDA_CLIENT_ID`, `REDPANDA_CLIENT_SECRET` | The organisation IAM client that creates the cluster and the PrivateLink endpoint. |

Fetch the value from your own secret store and export it before `tofu plan` /
`tofu apply`; never write either one into a `*.auto.tfvars.json`.

**`REDPANDA_CLIENT_ID`/`REDPANDA_CLIENT_SECRET` must be set for every plan
against this root, even an `msk` or `strimzi` one.** Verified against the real
provider binary: `redpanda` validates that a credential is PRESENT at provider
configure time, before OpenTofu knows whether `count` on `module.redpanda` is
0 or 1, so a plan with neither set fails with `no Client ID, Client Secret, or
Token found` regardless of `kafka.provider`. A placeholder value is enough
when redpanda-cloud is not the chosen provider -- nothing calls the API. The
`confluent` provider carries no equivalent requirement.

Two things this root does NOT do, both by construction:

- **The landing topics and the Kafka ACLs.** They live in the Kafka data plane,
  which OpenTofu has no client for and cannot reach in a private subnet. The
  bootstrap Job does them, authenticating by SASL/IAM -- the one path where the
  IAM policy is the authorisation and no ACL has to exist yet.
- **Render that Job.** The chart does, in the namespace and under the service
  account `kafka.msk.bootstrap_job` names. A Pod Identity association to a
  service account nothing renders grants nothing, and says so nowhere.

## Tearing it down

Order matters, and getting it wrong leaves resources billing that nothing can
find.

1. **Delete the Kubernetes workloads first.** Anything that made a load
   balancer, a volume or a DNS record did so through a controller, and those
   resources belong to AWS rather than to this state. `tofu destroy` does not
   know about them and a tag sweep does not find them.
2. `tofu destroy` here.
3. The `aws-state` bucket is marked `prevent_destroy`, deliberately. Removing it
   is a separate, deliberate act -- it holds the record of everything else.
