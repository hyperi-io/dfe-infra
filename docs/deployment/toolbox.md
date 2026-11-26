# Troubleshooting toolbox

Sometimes you need a shell inside the cluster's network. To curl a Service by its internal DNS name, run a query against ClickHouse without a port-forward, or poke at DNS from where a DFE pod sits. Two surfaces share one image for this: an opt-in in-cluster pod, and an on-demand EC2 bastion for AWS deployments. Both are off by default.

## The image

`docker/dfe-toolbox/` builds four images (`linux/amd64` and `linux/arm64`), pushed by `.github/workflows/toolbox-build.yml` to `ghcr.io/hyperi-io/dfe-toolbox-{base,aws,gcp,azure}`. All four are Debian `trixie-slim`, pinned by digest (`toolbox.debian`), run as uid/gid 1000, and share one entrypoint. gcloud and az each add close to 1 GB, so they stay out of the base. `-aws`, `-gcp` and `-azure` each `FROM` the base image and add one cloud's CLI on top.

Base: kubectl, helm, argocd (its CLI pinned to the ArgoCD *app* version `bootstrap.argocd`'s chart deploys, so it stays inside its supported skew), tofu, clickhouse-client (pinned to `services.clickhouse-version`, never a separate pin), psql, kcat (covers Kafka and Redpanda, where Apache's console scripts need a full Kafka install plus a JVM), and jq/yq/openssl/curl/dig/nc. There's no `tcpdump`. The pod is unprivileged with no `CAP_NET_RAW`/`CAP_NET_ADMIN`, so the binary would be dead weight.

Every version is a Docker build ARG defaulting to a `versions.yaml` `toolbox.*` pin. `scripts/check_versions_drift.py` holds each Dockerfile's default to its pin, and the build workflow overrides every one from the same source. A published family tag (`toolbox.dfe-toolbox`) never moves. `scripts/toolbox_inputs.py` builds an absent tag, skips a tag that already carries this build's inputs hash, and fails the plan for a tag carrying other inputs until the tag is bumped. On a pull request the same plan runs with no registry credential and pushes nothing, so a change that needs the bump fails before it reaches main. A stack block with no `toolbox.*` pins skips the build with a notice instead of failing.

Each platform builds on a runner of its own architecture, never under QEMU, and pushes by digest. A merge job joins the digests under the family tag with `docker buildx imagetools create`, which writes the inputs-hash annotation. The cloud images build `FROM` the base image by tag, so no cloud build starts until the base merge has pushed. The workflow reads the pins with the same reader the drift check uses, so it can't push a version the pin doesn't say.

Every download is checked before it installs, and a failed check fails the build:

- kubectl, tofu, clickhouse-client, gcloud and az come from the publisher's signed apt repo, with the repo key pinned by fingerprint in the Dockerfile and trusted for that repo alone.
- argocd and yq are copied out of their official images, pinned by index digest (`toolbox.argocd-cli-image`, `toolbox.yq-image`). The build fails unless each binary reports its release pin.
- helm, the AWS CLI and the Session Manager plugin are checked against a signature from a key pinned by fingerprint. AWS publishes its two keys in its docs alone, so they sit beside the aws Dockerfile.

Per-cloud: `-aws` adds the AWS CLI v2 and the Session Manager plugin, both at the pinned version. `-gcp` adds gcloud. `-azure` adds az from Microsoft's apt repo pinned to `bookworm`. `packages.microsoft.com` has no `trixie` suite yet, and the build itself is a self-contained venv that runs on trixie regardless.

Writable paths: `$HOME` (`/home/toolbox`) and `/tmp`, nothing else. The kubeconfig cache, helm's cache, `~/.aws`, `~/.config/gcloud` and `~/.azure` all land under `$HOME`. The root filesystem is read-only wherever this image runs. The in-cluster pod's `/work` mount, below, covers neither path.

## Bastion on AWS

If the in-cluster pod (below) can't help, the AWS bastion is the alternative. That's when the fault IS the Kubernetes API, or the API and brokers are reachable from inside the VPC alone. It's an EC2 instance that exists for as long as someone is using it, reached over Systems Manager Session Manager, never SSH. `terraform/modules/toolbox/aws` is the module, `dfe-ops bastion` is the operator surface, and `CONTRACT.md` there holds the exhaustive detail.

The instance is the resolver's `toolbox` entry in `shapes/compute-shapes.yaml` (the newest Graviton generation in the region), AL2023, in a private subnet, with no public IP and no inbound security-group rule at all. A different type goes in `sizing.overrides.toolbox.instance_type`, never in the dial. User-data installs the same tool list the image has, at the same pins. The instance is created on `bastion up` and TERMINATED (never stopped) on `bastion down`. A systemd timer on the instance counts active SSM sessions and self-terminates after `toolbox.ttl_minutes` (15-480, default 60) with none.

```yaml
toolbox:
  enabled: "false"          # dfe-ops bastion up/down flips this
  aws:
    shape_ref: toolbox      # a resolved shape, never an instance type
    operator_role_arn: ""   # required once enabled -- read-only EKS access
  ttl_minutes: 60
```

```bash
dfe-ops bastion up [--ttl MIN]      # read the newest AL2023 AMI, apply, wait for Online
dfe-ops bastion join [--ttl MIN]    # not available in this release
dfe-ops bastion peers               # the hub's peers and their last handshake
dfe-ops bastion hub <peer>          # reach one appliance through the tunnel
dfe-ops bastion shell               # a logged interactive shell
dfe-ops bastion forward <t> <port>  # a tunnel to a named target
dfe-ops bastion down                # revoke anything joined, then terminate
```

The four tunnel verbs exist where a deployment runs the edge-fleet VPN, and nowhere else. `hub` is the way to reach an appliance, and it routes the client range at the culvert pod rather than dialling in. `join` is not available in this release, because culvert drops an admin peer before any rule naming it is reached (https://github.com/hyperi-io/culvert/issues/40), so `down` has nothing to revoke. [edge.md](edge.md#the-tunnels-inbound-and-reaching-one-appliance) has the mechanism and what the appliance has to accept, and [edge-vpn.md](edge-vpn.md#reaching-an-appliance-from-the-bastion) what bounds the route.

## Forward targets

`forward` reaches a named target `bastion status` lists: the EKS API, one `kafka` target reaching a single bootstrap broker, `kafka-iam` on MSK's SASL/IAM listener, and ClickHouse when the dial names a host OUTSIDE the cluster. A Kubernetes Service name is never a target, because it resolves through CoreDNS and the instance sits outside the cluster. So an in-cluster ClickHouse is reached with `kubectl port-forward` from `bastion shell`, over the EKS API grant below.

A target whose address the deployment didn't create inside the VPC (a ClickHouse Cloud host, a Confluent Cloud or Redpanda Cloud bootstrap) is opened to `0.0.0.0/0` on its port rather than to the VPC CIDR. A rule aimed inside the VPC leaves the forward hanging with nothing to report it. Each target's key and port are known at plan time, so the toolbox can be enabled in the dial before the first apply. Reaching a broker tests reachability, TLS and SASL.

A client that must follow metadata to the other brokers (most Kafka clients do) runs on the instance instead, via `bastion shell`, because the advertised hostnames resolve inside the VPC alone. AL2023 publishes no `kcat` package, so the instance has the Apache Kafka project's console scripts instead (`kafka-console-producer.sh`, `kafka-console-consumer.sh`, `kafka-topics.sh`, `kafka-consumer-groups.sh`, `kafka-broker-api-versions.sh`). They're pinned to the SAME broker version this deployment runs, plus the Java 17 runtime they need (`terraform/modules/toolbox/aws/CONTRACT.md`, "Kafka CLI on the EC2 instance"). The container image's `kcat` remains what the in-cluster pod (below) uses, because Debian's repos have it.

Each target has a Session document of its OWN with the host and port fixed in the document body. `--parameters host=...` against one fails as an unknown parameter, and that's why a forward can't reach the instance's metadata service or an arbitrary external host. Forwarding to the EKS API also writes a scratch kubeconfig (`.tmp/toolbox-eks-api.kubeconfig`, mode `0600`, deleted on `down`, never merged into `~/.kube/config`).

## Access and cost

The instance itself gets NO EKS access entry. A forward's kubectl identity is the operator's role, authenticated at the laptop end of the tunnel. `toolbox.aws.operator_role_arn` is instead granted a read-only (`AmazonEKSViewPolicy`) access entry, so it's read-only by default and never cluster-admin. Starting a session at all is IAM you provide, not IAM this module creates. `CONTRACT.md` gives the exact policy JSON needed.

Cost is XS on the scale in [aws.md](aws.md#how-costs-are-described): one small Graviton instance billed hourly plus its root volume, on the cluster VPC's existing NAT for its 443 egress. Zero while `toolbox.enabled` is false, apart from the session-log bucket, which persists across an up/down cycle by design.

## In-cluster pod

`helm/charts/dfe-toolbox` deploys a single-container Deployment that runs `sleep infinity` and nothing else, on every cluster, cloud or on-prem. It deploys the same way `network-policies` does, with no cloud gate. What decides whether it does anything is `toolbox.pod.enabled`, off everywhere by default:

```yaml
toolbox:
  pod:
    enabled: false        # replicas: 0 -> 1
    kubeApiAccess: false  # a read-only Kubernetes credential, see below
    idleSeconds: ""       # scale to zero after this many seconds without activity
    idleWarnSeconds: 300  # notice into every open terminal this long before
    ttlSeconds: ""        # hard ceiling from the pod's start, whatever the activity
```

The dial (`deployment.yaml`'s `toolbox.pod.*`) turns it on. It reaches the chart through `argocd/appsets/layer2-platform.yaml`'s own parameters and wins over `argocd/values/common.yaml`'s fleet-wide default. With `enabled: true` the Deployment runs one replica, and you reach it with `kubectl exec -it -n default deploy/dfe-toolbox -- /bin/sh`. It runs non-root, with a read-only root filesystem and no capabilities. `/work` is an `emptyDir` for anything written to disk, on top of the image's `$HOME`/`/tmp` (above).

`kubeApiAccess: true` (rejected unless `enabled` is true as well) adds a dedicated ServiceAccount and ClusterRole, bound into the DFE app namespace, the data namespaces (`cnpg`, `strimzi`, `clickhouse`) and `otel`. Never cluster-wide, and never `pods/exec`. Exec is how an operator gets INTO a pod, granted by the operator's identity, never the toolbox pod's credential.

The chart's `NetworkPolicy` limits egress to cluster DNS and those same namespaces, but that's a request to the CNI, not a control that enforces itself. On EKS, the `vpc-cni` add-on sets `enableNetworkPolicy` so the policy is enforced. On-prem it depends on the cluster's CNI supporting `NetworkPolicy` at all. Confirm which applies before relying on the fence, especially alongside `kubeApiAccess: true`.

With `idleSeconds` or `ttlSeconds` set, the `dfe-toolbox-ttl` CronJob checks the pod every minute and scales the Deployment to zero. Both empty, the pod runs until an operator disables it.

- `idleSeconds`: no activity for that long. A command at a prompt, a shell opened with `kubectl exec` and a command still running (a long query, a log tail) all count. An untouched pod counts from its start.
- `ttlSeconds`: a hard ceiling from the pod's start, so a busy pod is still scaled at the limit.
- `idleWarnSeconds`: one notice into every open terminal this long before either scale-down. `0` skips it.

The image is unchanged. A ConfigMap mounts five scripts at `/opt/dfe-idle`, and the pod's `ENV` and `PROMPT_COMMAND` make an interactive `sh` or `bash` write the epoch to `/tmp/.dfe-toolbox-activity` at start and at each prompt. The CronJob reads it with `kubectl exec`. A shell that replaces `PROMPT_COMMAND`, or `kubectl exec -- <cmd>` with no shell, goes unseen.

The CronJob's ServiceAccount holds `pods/exec` on the namespace's pods, since the pod name carries a ReplicaSet hash. The toolbox pod's own credential never gets exec. A pod that cannot be probed counts as busy, so only `ttlSeconds` scales it.

Argo CD's `selfHeal` would put `replicas: 1` straight back, so the scale-down is a patch of the Deployment under the field manager `dfe-toolbox-idle`, and `layer2-platform.yaml` ignores differences on the replicas that manager owns. A write through the `scale` subresource leaves no field-manager entry to ignore, so the CronJob holds `patch` on the Deployment itself, fenced to its name.

To run the toolbox again, patch the Deployment as the default field manager, never `dfe-toolbox-idle`:

```bash
kubectl patch deployment/dfe-toolbox -n default --type=merge -p '{"spec":{"replicas":1}}'
```

That hands the field back to Argo CD, so a later `toolbox.pod.enabled: false` still scales it down. Setting `enabled` to false and then true works too, because the Deployment carries the value in the `dfe.hyperi.io/toolbox-enabled` annotation and Argo CD syncs the change. Any other sync of the Deployment, such as a chart upgrade, also puts the replicas back to what the dial says.

## What is recorded, and what is not

A bastion shell session (`bastion shell`) is logged to the deployment's session-log prefix, encrypted with the deployment's key, and kept for `toolbox.session_log_retention_days` (90 when the dial is silent). That's independent of the telemetry dial's 2-7 day window, because a session log is evidence of human access to a customer's private data plane, not service telemetry.

AWS Session Manager records no content for a port-forward session, so a bastion `forward` session is NOT recorded. CloudTrail's `StartSession` event, naming the per-target Session document, is the compensating record. Each forward target gets its own document, so the document name alone says which target was reached, whether or not CloudTrail logs the forward's parameters as well (not verified against a live account).

The EKS control-plane audit log narrows the gap further for Kubernetes traffic. Both an EKS-API forward and an in-cluster pod's `kubectl exec` show up there, attributed to the operator's identity, even though the tunnel or the exec session itself carries no transcript. Nothing does this for Kafka or ClickHouse. A Kafka consume or a `clickhouse-client` query through either surface leaves no record anywhere in this deployment.
