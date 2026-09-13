# toolbox/aws -- module contract

The on-demand, SSM-managed troubleshooting instance for an AWS-provisioned DFE
deployment: `t4g.small` by default, AL2023 arm64, no public IP, no inbound
security-group rule of any kind, terminated (never stopped) the moment nobody
is using it. `dfe-ops bastion up`/`down` is the operator surface; this module
is what `up`/`down` toggle.

The numbered references below (`#1`-`#18`) index a set of AWS access-control
findings for an SSM-managed toolbox -- not a public standard, just this file's
own cross-reference scheme, kept stable so a comment in `main.tf` pointing at
`#7` resolves to something readable here. Not every one of the eighteen is
this module's to close; see "Out of scope" at the end.

## Inputs

| Input | Type | Meaning |
|-------|------|---------|
| `name`, `env` | `string` | Name prefix and environment, as everywhere else in this repo. |
| `enabled` | `bool` | Whether the toolbox INSTANCE exists. `false` tears down the instance, its security group, its instance profile and both kinds of Session document. Does NOT gate the session-log bucket -- see below. |
| `network` | `object({ vpc_id, cidr, private_subnet_ids })` | Where the instance lands. `cidr` scopes the target-specific egress rules; every named target lives inside the VPC by construction. |
| `instance_type` | `string` | `toolbox.aws.instance_type` in the dial. Validated as a Graviton (arm64) family. |
| `ttl_minutes` | `number` | Idle-session bound, 15-480, enforced here AND by `dfe-ops`. |
| `tool_versions` | `map(string)` | `kubectl`, `helm`, `argocd-cli`, `tofu`, `yq`, `aws-cli`, `aws-session-manager-plugin`, `clickhouse-client`, `psql` -- every key required and non-empty. Assembled by `render_dial.py` from `versions.yaml`; see "Where the tool versions come from" below. |
| `session` | `object({ idle_timeout_minutes, max_duration_minutes })` | The SHELL document's preferences only. |
| `session_log_retention_days` | `number` | Default 90. Deliberately independent of `telemetry.retention_days` -- `#18`. |
| `kms_key_arn` | `string` | The deployment CMK. Used via an IAM role policy, never a key policy of this module's own -- `#6` (see "What this module deliberately does NOT write"). |
| `targets` | `map(object({ host, port }))` | Named forward targets, computed by the aws root from ITS OTHER modules' outputs. Host and port are fixed at plan time -- `#1`, `#7`. |
| `force_destroy_session_logs` | `bool` | Follows the root's `tags.lifecycle` the way `cloudtrail.tf`'s bucket does. |
| `tags` | `map(string)` | The governance tag set. Merged with `dfe.hyperi.io/component = toolbox` on every resource -- the tag every IAM condition example below scopes against. |

## Outputs

| Output | Meaning |
|--------|---------|
| `instance_id` | Empty when not enabled. |
| `ssm_session_document` | The SHELL document's name. |
| `targets` | Each named target plus its forward document's name. |
| `session_log_bucket` | Persists across every up/down cycle. |
| `security_group_id`, `iam_role_name` | For a `down` proof and for policy examples; empty when not enabled. |

## Why the session-log bucket is not gated by `enabled`

Every other resource in this module exists only while `enabled` is true, using
the SAME `count = var.enabled ? 1 : 0` pattern the aws root already uses for
`module.kafka`/`module.confluent`/`module.redpanda` -- except this module is
never conditionally INSTANTIATED at the root (no `count` on the `module
"toolbox"` block itself); `enabled` is threaded through as a plain input
instead. That is the one deliberate difference from the kafka bodies' own
convention, and it exists for exactly one reason: `#18` requires the
session-log bucket to carry its OWN 90-day retention, and a `bastion down`
that also deleted the bucket would erase the evidence of the very session it
is closing out, days before that retention window is up. Gating the module's
INSTANTIATION on `enabled` would do exactly that, because every resource
inside an uninstantiated module is destroyed with it. Gating each resource
individually, with the bucket left out, is what lets the bucket survive an
`up`/`down` cycle and disappear only when the whole deployment is torn down.

## What this module deliberately does NOT write

- **No `aws_kms_key_policy`.** `kubernetes-cluster/aws/kms.tf` is the
  deployment CMK's one policy owner (`aws_kms_key_policy` REPLACES a key's
  whole policy, so a second one anywhere else silently strips whatever the
  first wrote -- this is the exact bug `managed-kafka/msk`'s own copy used to
  carry, fixed by moving its log-delivery grant into the root's
  `key_policy_grants`). This module needs no equivalent: `kms.tf`'s key policy
  already delegates to IAM via an account-root statement, which is what makes
  a plain `aws_iam_role_policy` on this module's own instance role (`main.tf`,
  `aws_iam_role_policy.session_log`) sufficient on its own. The
  `key_policy_grants` mechanism exists only for an AWS SERVICE PRINCIPAL with
  no IAM identity to attach a policy to (CloudTrail, MSK's broker-log
  delivery) -- this module's caller for the key IS an IAM identity, so that
  mechanism does not apply here at all. `#6`.
- **No `ec2:TerminateInstances`, no `ssm:DescribeSessions`.** `#9`: the
  self-terminate mechanism is `instance_initiated_shutdown_behavior =
  "terminate"` plus a local process count (the SSM Agent forks one
  `ssm-session-worker` per active session, shell or forward alike), which
  needs neither grant. `ec2:TerminateInstances` scoped to "this instance's own
  ARN" is also a dependency cycle in tofu (the role policy would have to name
  the instance id, and the instance references the instance profile that
  carries the role) -- the workaround would be a tag condition, which is
  WEAKER (any toolbox-tagged instance, not just this one), so it is not built
  at all rather than built weak.
- **No `ReadOnlyAccess`.** `#4`: that AWS-managed policy reaches the
  deployment's CloudTrail bucket and the MSK broker-log bucket too -- the
  audit record and the broker logs, readable from the one box whose own
  forwards are unlogged. The instance profile carries exactly
  `AmazonSSMManagedInstanceCore`, `AmazonEC2ContainerRegistryPullOnly`, and
  the inline policy above.
- **No `--parameters host=...`/`portNumber=...` on any forward document.**
  `#1`, `#7`: `AWS-StartPortForwardingSessionToRemoteHost` takes host and port
  as CALLER-SUPPLIED parameters, and IAM has no condition key for a session
  document's parameter VALUES -- a grant that permits that document permits a
  tunnel to anything the instance can reach, including
  `169.254.169.254:80` (the instance's own IMDS; IMDSv2 does not stop this,
  because the request originates ON the instance) or an external host (an
  unlogged egress proxy out of the VPC). This module instead creates one
  CUSTOM `sessionType: Port` document PER entry in `var.targets`, with `host`
  and `portNumber` written as literal values in the document body and NO
  `host`/`portNumber` parameter declared at all -- `--parameters host=x`
  against one of these documents is rejected as an unknown parameter, not
  merely discouraged by convention. Only `localPortNumber` (which local port
  on the OPERATOR's machine) is a real parameter.

## Session logging -- what is recorded and what is not (`#6`)

Session Manager writes SHELL session transcripts to S3 (this module's
`s3BucketName`/`s3KeyPrefix` on the shell document). **A port-forward session
produces no transcript, by AWS's own design** -- there is nothing in Session
Manager's own preferences to turn on that would change this; see
<https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-logging.html>.
So the primary reach of this toolbox -- `kubectl` against the private EKS API,
`kcat` against a broker, `clickhouse-client` against ClickHouse -- is exactly
the half that is not recorded as a transcript.

Three things narrow that gap, none of them a substitute for a transcript:

1. **CloudTrail records `StartSession` as a management event**
   (`terraform/environments/aws/cloudtrail.tf`, unconditional under both
   telemetry sinks), with the caller identity, source IP and `documentName`.
   Because this module names one document PER target, `documentName` alone
   says WHICH target was reached, without depending on whether AWS also logs
   the forward's parameters -- that has not been verified against a live
   account. A real `StartSession` event needs to be captured and inspected
   before any doc states parameter-level logging as fact rather than an
   assumption.
2. **The EKS control-plane audit log stays on** under both telemetry sinks
   (`kubernetes-cluster/aws/eks.tf`, `enabled_cluster_log_types = ["audit"]`),
   so a `kubectl` call through the EKS-API forward is recorded, attributed to
   the OPERATOR's own identity (the tunnel terminates TLS at the laptop -- see
   "EKS access" below) -- even though the tunnel that carried it is not.
3. **Nothing does this for Kafka or ClickHouse.** That gap is real and stays
   named, not glossed, in `docs/deployment/toolbox.md`.

## Session-log retention is its own decision, not `telemetry.retention_days` (`#18`)

Q48's rule is "DFE's own monitoring goes to OTel and HyperDX, never
CloudWatch", with `telemetry.aws.retention_days` defaulting to 2 (`otel`) or 7
(`cloudwatch`) -- and `kubernetes-cluster/aws/eks.tf` puts the EKS audit log
at 1 day under `otel`. A one-to-two-day window is fine for service telemetry
and useless as a record of who was on a customer's private data plane and
when. `session_log_retention_days` (default 90) is a deliberate, named
exception to Q48 for exactly that reason -- a session log is evidence, not
telemetry. No Object Lock: the bucket is torn down with the deployment, and
Object Lock fights ephemeral teardown the same way it would on any other log
bucket in this repo.

## EKS access -- the toolbox instance gets none

The instance itself carries **no EKS access entry**. The tunnel a `forward`
opens terminates TLS at the OPERATOR's laptop -- the kubectl identity making
API calls through it is the operator's own IAM identity, authenticated the
normal `aws eks get-token` way, never the instance's role. Binding an access
entry to the instance profile would create a SECOND, weaker path to the API
that survives the operator logging out, which is why this module has no
`aws_eks_access_entry` resource of its own at all. The aws root
(`terraform/environments/aws/main.tf`) is what grants the OPERATOR a
read-only (`AmazonEKSViewPolicy`) access entry, gated on the same `toolbox`
dial toggle -- see that root's own comments for why this lives there rather
than in `kubernetes-cluster/aws/eks.tf` (`#5`/`#11`; `eks.tf` is a sibling
module's file, not this module's to edit, and the access entry needs no
change to it -- it is a standalone resource against the cluster by name).

## Where the tool versions come from

`render_dial.py` builds `tool_versions` from two places in `versions.yaml`,
never from a literal in this module or its `templates/user_data.sh.tftpl`:

- `kubectl`, `helm`, `argocd-cli`, `tofu`, `yq`, `aws-cli`,
  `aws-session-manager-plugin` come from the `toolbox:` stage under the
  current stack -- the SAME stage `docker/dfe-toolbox/base/Dockerfile` and
  `docker/dfe-toolbox/aws/Dockerfile` pin their own `ARG` defaults from
  (`scripts/check_versions_drift.py` holds the two together on the image
  side), so the EC2 instance and the container image can never drift apart.
  If a customer's `versions.yaml` ever carries no `toolbox:` stage at all
  (an old fork, or a stack cut before this stage existed), `render_dial.py`
  reads an empty map for this half (see its own `_toolbox_tool_versions()`),
  and this module's `tool_versions` validation refuses a plan with
  `toolbox.enabled: true` by naming exactly which keys are missing, rather
  than shipping a box with no pinned tools.
- `clickhouse-client` reads `services.clickhouse-version` and `psql` reads
  `services.postgresql` -- the SAME keys the deployed ClickHouse server and
  CNPG cluster already pin, so the debugging tool's protocol version can never
  skew from the server it is debugging. The `toolbox:` stage deliberately
  carries no pin of its own for either, for exactly this reason (its own
  comment says so).
- `jq`, `kcat` and `openssl` are a deliberate exception: none carries an
  upstream release cadence worth tracking as a versions.yaml pin (the
  `toolbox:` stage's own comment says so for `kcat` specifically -- no
  release since 1.7.1 in 2021), so all three install unpinned via `dnf`,
  matching `docker/dfe-toolbox/base/Dockerfile`'s identical choice to install
  them unpinned via `apt`.

## AMI resolution (`#8`)

Resolved through the SSM public parameter
`/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64`
(<https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/finding-an-ami-parameter-store.html>)
-- never a literal AMI id. Deliberately the ALWAYS-LATEST alias, not a dated
pin the way `karpenter-pools/values.yaml`'s `amiAlias: al2023@v20260827` is.
That pin exists because a Karpenter node is long-lived and Karpenter
reconciles continuously, so an unpinned "latest" alias there means the running
fleet's image can drift mid-life with no version an operator could point at.
This instance has no such lifecycle: it is created fresh on every `bastion
up` and terminated on every `bastion down`, so there is no "already-running
box whose image silently moved" failure mode to guard against, and the
correct behaviour for an on-demand debugging box is to always carry whatever
AL2023 currently ships. AWS also changed this exact parameter's own behaviour
on 2026-08-17 to always track the latest kernel rather than staying fixed at
its original one -- consistent with treating "latest at the moment you bring
it up" as the intended semantics here, not a gap to close later.

IMDSv2 required, hop limit 1 (`metadata_options`), matching
`karpenter-pools/values.yaml`'s node-level setting verbatim. Per `#7`, this
does NOT protect the instance metadata service from a port-forward tunnel --
that connection originates ON the instance itself, so the hop limit is
satisfied and a caller holding the (denied, per `#1`) AWS-managed forward
document could still complete the IMDSv2 token exchange over the raw tunnel.
It protects the metadata service from a process running locally on the box,
which is the threat IMDSv2 was built for.

## What this module leaves to the customer/deployer's own IAM

This module creates no IAM permission set for a human operator, and the
`ssm:StartSession`/`ssm:TerminateSession`/`ssm:ResumeSession` grants below
live on the OPERATOR's own role or SSO permission set, which is outside this
module's (and this repo's) reach -- there is no existing AWS IAM principal or
permission-set definition anywhere in this repo to extend. The policy an
operator's role needs, in full:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "StartSessionOnTheToolboxOnly",
      "Effect": "Allow",
      "Action": "ssm:StartSession",
      "Resource": [
        "arn:aws:ec2:<region>:<account>:instance/*",
        "arn:aws:ssm:<region>:<account>:document/<name>-toolbox-shell",
        "arn:aws:ssm:<region>:<account>:document/<name>-toolbox-forward-*"
      ],
      "Condition": {
        "StringEquals": { "ssm:resourceTag/dfe.hyperi.io/component": "toolbox" },
        "BoolIfExists": { "ssm:SessionDocumentAccessCheck": "true" }
      }
    },
    {
      "Sid": "DenyTheAwsManagedForwardAndDefaultDocuments",
      "Effect": "Deny",
      "Action": "ssm:StartSession",
      "Resource": [
        "arn:aws:ssm:*:*:document/AWS-StartPortForwardingSession*",
        "arn:aws:ssm:*:*:document/AWS-StartSSHSession",
        "arn:aws:ssm:*:*:document/SSM-SessionManagerRunShell"
      ]
    }
  ]
}
```

The `ssm:resourceTag/dfe.hyperi.io/component = toolbox` condition on the
INSTANCE half is not optional: the cluster's EKS worker nodes already carry
`AmazonSSMManagedInstanceCore` (`kubernetes-cluster/aws/eks.tf`), so an
unconditioned `ssm:StartSession` grant on `instance/*` is a root shell on any
worker node, not just this toolbox. `ssm:SessionDocumentAccessCheck` is the
documented control that stops a grant like the one above being satisfied by
falling back to `SSM-SessionManagerRunShell` when no `--document-name` is
given -- without it, `ssm:StartSession` permission on the instance ALONE is
enough to reach the account default document even with no `Resource` entry
naming it
(<https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-sessiondocumentaccesscheck.html>).

## Rules this module follows

- **Zero ingress rules, unconditionally.** Not "0.0.0.0/0 on some port
  removed" -- no `aws_vpc_security_group_ingress_rule` resource exists in
  this module at all.
- **`down` is a terminate, never a stop.** `instance_initiated_shutdown_behavior
  = "terminate"` plus `count = var.enabled ? 1 : 0` on the instance resource
  itself: the instance does not exist when `enabled` is false, and a stopped
  instance (which still bills for its volume) is never a state this module
  can be in.
- **Every tool version is a lookup, never a literal**, in this module and in
  `templates/user_data.sh.tftpl` alike.

## Known gap, stated rather than hidden

`kcat` is deliberately unpinned (see "Where the tool versions come from"),
but AL2023's own `dnf` repos may not publish a `kcat` package at all, unlike
Debian, which does -- this was not verified against a live AL2023 instance
at the time this module was written. The user data template's
`dnf install -y kcat || true` may install nothing. This is the one install
step that needs proving against a real instance before it is relied on. The
container image's own `kcat` install (Debian's `kcat` apt package) does not
have this gap.

## Out of scope for this module

NetworkPolicy enforcement on the `vpc-cni` add-on (`enableNetworkPolicy`) and
the in-cluster toolbox pod's own egress fence belong to the sibling
workstream building the in-cluster toolbox chart and appset, and to whichever
change eventually touches `kubernetes-cluster/aws/eks.tf` -- neither is this
module's file to edit, and neither is this EC2 bastion's own concern: the
pod's NetworkPolicy fence has nothing to do with an instance that carries no
Kubernetes workload at all. Flagged back rather than silently applied or
silently dropped.
