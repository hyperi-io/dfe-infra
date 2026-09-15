# Troubleshooting toolbox

Sometimes an operator needs a shell inside the cluster's own network -- to
curl a Service by its internal DNS name, run a query against ClickHouse
without a port-forward, or poke at DNS resolution from where a DFE pod
actually sits. Two surfaces share one image for this: an opt-in in-cluster
pod, and an on-demand EC2 bastion for AWS deployments. Both are off by
default.

## The image

`docker/dfe-toolbox/` builds four images (`linux/amd64` and `linux/arm64`),
pushed by `.github/workflows/toolbox-build.yml` to
`ghcr.io/hyperi-io/dfe-toolbox-{base,aws,gcp,azure}`. All four are Debian
`trixie-slim`, run as uid/gid 1000, and share one entrypoint. gcloud and az
each add close to 1 GB, so they stay out of the base -- `-aws`, `-gcp` and
`-azure` each `FROM` the base image and add one cloud's CLI on top.

**Base:** kubectl, helm, argocd (its CLI pinned to the ArgoCD *app* version
`bootstrap.argocd`'s chart deploys, so it stays inside its supported skew),
tofu, clickhouse-client (pinned to `services.clickhouse-version`, never a pin
of its own), psql, kcat (covers Kafka and Redpanda -- Apache's own console
scripts need a full Kafka install plus a JVM), and jq/yq/openssl/curl/dig/nc.
No `tcpdump`: the pod is unprivileged with no `CAP_NET_RAW`/`CAP_NET_ADMIN`,
so the binary would be dead weight.

Every version is a Docker build ARG defaulting to a `versions.yaml`
`toolbox.*` pin; `scripts/check_versions_drift.py` holds each Dockerfile's
default to its pin, and the build workflow overrides every one from the same
source.

**Per-cloud:** `-aws` adds the AWS CLI v2 and the Session Manager plugin --
AWS publishes no versioned plugin download, only a floating `latest`, so the
build asserts the installed version still matches the recorded pin. `-gcp`
adds gcloud. `-azure` adds az from Microsoft's apt repo pinned to `bookworm`
(`packages.microsoft.com` has no `trixie` suite yet; the build itself is a
self-contained venv that runs on trixie regardless).

**Writable paths:** `$HOME` (`/home/toolbox`) and `/tmp` only -- kubeconfig
cache, helm's cache, `~/.aws`, `~/.config/gcloud` and `~/.azure` all land
under `$HOME`. The root filesystem is read-only wherever this image runs. The
in-cluster pod's own `/work` mount, below, covers neither path.

## Bastion on AWS

If the in-cluster pod (below) cannot help -- the fault IS the Kubernetes API,
or the API and brokers are only reachable from inside the VPC -- the AWS
bastion is a standing-nothing alternative: an EC2 instance that exists only
while someone is using it, reached over Systems Manager Session Manager,
never SSH.
`terraform/modules/toolbox/aws` is the module; `dfe-ops bastion` is the
operator surface; `CONTRACT.md` there holds the exhaustive detail.

`t4g.small` (Graviton) by default, AL2023, private subnet, no public IP, no
inbound security-group rule at all. User-data installs the same tool list the
image carries, at the same pins. The instance is created on `bastion up` and
TERMINATED (never stopped) on `bastion down`; a systemd timer on the instance
counts active SSM sessions and self-terminates after `toolbox.ttl_minutes`
(15-480, default 60) with none.

```yaml
toolbox:
  enabled: "false"          # dfe-ops bastion up/down flips this
  aws:
    instance_type: t4g.small
    operator_role_arn: ""   # required once enabled -- read-only EKS access
  ttl_minutes: 60
```

```
dfe-ops bastion up [--ttl MIN]      # apply, wait for Online
dfe-ops bastion shell               # a logged interactive shell
dfe-ops bastion forward <t> <port>  # a tunnel to a named target
dfe-ops bastion down                # terminate, then PROVE nothing remains
```

`forward` reaches a named target `bastion status` lists: the EKS API, one
`kafka` target reaching a single bootstrap broker, and ClickHouse when the
dial names a host. `kafka`'s key and port are known at plan time, so the
target can be enabled in the dial before the first apply, and reaching it
proves reachability, TLS and SASL. A client that
must follow metadata to the other brokers -- most real Kafka clients do --
runs on the instance instead, via `bastion shell`, because the advertised
hostnames resolve only inside the VPC. `kcat` is there where AL2023's repos
publish one: that install is unpinned and deliberately non-fatal, the only one
on the instance that can no-op silently
(`terraform/modules/toolbox/aws/CONTRACT.md`).

Each target has its OWN Session document with the host and port fixed in the
document body -- `--parameters host=...` against one fails as an unknown
parameter -- the reason a forward cannot reach the instance's own metadata
service or an arbitrary external host. Forwarding to the EKS API also writes
a scratch kubeconfig (`.tmp/toolbox-eks-api.kubeconfig`, mode `0600`, deleted
on `down`, never merged into `~/.kube/config`).

The instance itself gets NO EKS access entry -- a forward's kubectl identity
is the operator's own role, authenticated at the laptop end of the tunnel.
`toolbox.aws.operator_role_arn` is instead granted a read-only
(`AmazonEKSViewPolicy`) access entry: read-only by default, never
cluster-admin. Starting a session at all is IAM you provide, not IAM this
module creates -- `CONTRACT.md` gives the exact policy JSON needed.

Roughly the price of one `t4g.small` (~USD 0.02/hr) plus its root volume, on
the cluster VPC's existing NAT for its 443 egress. Zero cost while
`toolbox.enabled` is false, apart from the session-log bucket, which persists
across an up/down cycle by design.

## In-cluster pod

`helm/charts/dfe-toolbox` deploys a single-container Deployment that runs
`sleep infinity` and nothing else, on every cluster cloud or on-prem -- the
same way `network-policies` deploys, with no cloud gate. What decides whether
it does anything is `toolbox.pod.enabled`, off everywhere by default:

```yaml
toolbox:
  pod:
    enabled: false        # replicas: 0 -> 1
    kubeApiAccess: false  # a read-only Kubernetes credential, see below
    ttlSeconds: ""        # auto-scale back to zero after this many seconds
```

The dial (`deployment.yaml`'s `toolbox.pod.*`) is what turns it on -- it
reaches the chart through `argocd/appsets/layer2-platform.yaml`'s own
parameters and wins over `argocd/values/common.yaml`'s fleet-wide default.
With `enabled: true` the Deployment runs one replica; reach it with `kubectl
exec -it -n default deploy/dfe-toolbox -- /bin/sh`. It runs non-root, with a
read-only root filesystem and no capabilities; `/work` is an `emptyDir` for
anything written to disk, on top of the image's own `$HOME`/`/tmp` (above).

`kubeApiAccess: true` (refused unless `enabled` is also true) adds a
dedicated ServiceAccount and ClusterRole, bound into the DFE app namespace,
the data namespaces (`cnpg`, `strimzi`, `clickhouse`) and `otel` -- never
cluster-wide, and never `pods/exec`: exec is how an operator gets INTO a pod,
granted by the operator's own identity, never the toolbox pod's own
credential.

The chart's `NetworkPolicy` limits egress to cluster DNS and those same
namespaces, but that is a request to the CNI, not a control that enforces
itself. On EKS, the `vpc-cni` add-on now sets `enableNetworkPolicy` so the
policy is real; on-prem it depends on the cluster's own CNI supporting
`NetworkPolicy` at all -- confirm which applies before relying on the fence,
especially alongside `kubeApiAccess: true`.

`ttlSeconds`, when set, renders a companion CronJob that checks the pod's own
`status.startTime` once a minute and scales the Deployment back to zero past
that many seconds -- left empty, the pod runs until an operator disables it
by hand.

## What is recorded, and what is not

A bastion shell session (`bastion shell`) is logged to the deployment's own
session-log prefix, encrypted with the deployment's key, kept for
`toolbox.session_log_retention_days` (default 90) -- independent of the
telemetry dial's 2-7 day window, because a session log is evidence of human
access to a customer's private data plane, not service telemetry.

A bastion `forward` session is NOT recorded by Session Manager, and this is
not a gap in this repo's own configuration: Session Manager records no
content for a port-forward session at all, by AWS's own design. CloudTrail's
`StartSession` event, naming the per-target Session document, is the
compensating record -- because each forward target gets its own document,
the document name alone says which target was reached, whether or not
CloudTrail also logs the forward's own parameters (not verified against a
live account). The EKS control-plane audit log narrows the gap further for
Kubernetes traffic specifically: both an EKS-API forward and an in-cluster
pod's `kubectl exec` show up there, attributed to the operator's own
identity, even though the tunnel or the exec session itself carries no
transcript. Nothing does this for Kafka or ClickHouse -- a `kcat` consume or
a `clickhouse-client` query through either surface leaves no record anywhere
in this deployment.
