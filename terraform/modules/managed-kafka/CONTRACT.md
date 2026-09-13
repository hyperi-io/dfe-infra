# managed-kafka -- module contract

The Kafka a deployment gets when it does not run its own. One body per
VENDOR, not per cloud, because only one of them is cloud-bound:

| Body | Status | What it is | Clouds |
|------|--------|------------|--------|
| `msk/` | built | Amazon MSK Provisioned, Express brokers | AWS only |
| `redpanda-cloud/` | built, AWS half | Redpanda Cloud Serverless on AWS PrivateLink | AWS; the vendor offers Serverless private connectivity nowhere else |
| `confluent-cloud/` | built, AWS half | Confluent Cloud on Confluent's own private networking | AWS; GCP and Azure need their own second provider |
| `strimzi/` | not built | Strimzi in the cluster we already have | any, including on-prem |

Every body takes the inputs below, produces the outputs below, and passes the
contract test in `tests/`. A body that needs an input the contract does not name
is a contract change, not a body detail: add it here first, then to every body,
then to the test.

There is one test FILE per body -- `contract.tftest.hcl` for `msk/`,
`contract-<body>.tftest.hcl` for the rest -- and not by preference. A file-level
`mock_provider` has to resolve against the `required_providers` of every run's
module in that file, so a `mock_provider "redpanda"` sitting beside a run
against `./msk` resolves to a `hashicorp/redpanda` that does not exist. The
assertions are the same in each file; only the mocks differ.

MSK is the only cloud-native Kafka DFE supports. On GCP and Azure the answer is
an egress-safe Redpanda Cloud or Confluent Cloud, through that vendor's own
provider -- so the contract treats SaaS-with-private-connectivity as the primary
shape and MSK as the one native implementation, rather than the reverse.

## Two providers, designed in now

A SaaS body configures TWO providers: the vendor's, which creates the cluster,
and the cloud's, which accepts the private-link attachment from its side. The
handshake has two ends and neither provider can do the other's half. That is
declared here before any body is written because retrofitting a second provider
into a module is a rewrite, not an edit.

`msk/` and `strimzi/` each need one provider, and satisfy the contract without
using the second.

## Inputs

| Input | Type | Meaning |
|-------|------|---------|
| `implementation` | `string` | `msk`, `redpanda-cloud`, `confluent-cloud` or `strimzi`. Selects the body; each body asserts it is the one named. |
| `connectivity` | `string` | `private` (the default, and the only one a product deploy should use) or `public`. |
| `name` | `string` | Name prefix for every resource the body creates, and the cluster's own name. |
| `env` | `string` | Deployment environment, for the names and descriptions that have to distinguish two deployments in one account. |
| `network` | the cluster module's `network` output | `{ vpc_id, cidr, azs, private_subnet_ids, public_subnet_ids }`. Where the brokers attach, or what the private link reaches. |
| `broker_shape_ref` | `string` | Indexes the resolver's shapes, exactly as node pools do. MSK's broker namespace is its own (`express.m7g.large`) and runs generations behind EC2, so the resolver answers it separately. |
| `resolved_shapes` | `map(object({ instance_types, arch }))` | The resolver's answer, keyed by `shape_ref`. An instance type is never written by hand. |
| `broker_count` | `number` | Three at the floor, and a multiple of the availability-zone count. Replication factor and minimum in-sync replicas are the profile's, not this module's. |
| `kafka_version` | `string` | The broker version, in the provider's own spelling. No default: the newest supported version is read from the vendor at build time, because a default rots into a deprecated line silently. |
| `client_cidrs` | `list(string)` | Who may reach the brokers. Empty means the whole VPC the cluster module built. |
| the tuning settings | `number` each | `num_partitions`, `log_retention_ms`, `message_max_bytes`. The canonical broker profile, already reduced to what this implementation accepts -- named and validated rather than an opaque map, because a map cannot be checked against a provider's read-only list and a setting silently dropped is the failure mode this contract exists to prevent. A body APPLIES what it is given and reports what its provider refuses; it does not decide what is applicable. |
| `kms_key_arn` | `string` | The deployment's own key, from the cluster module. Encrypts the data at rest and the credential. |
| `eks_cluster_name`, `pod_identity`, `pod_identity_trust_policy_json` | | The workload identity the bootstrap Job runs as, for a body whose provider needs one. |
| `autoscaling` | `object({ enabled, max_brokers, step, per_broker_capacity_mb_s, headroom })` | `msk`-only: broker-count scaling, since AWS gives Express no native equivalent. Ignored by both SaaS bodies, whose vendor already elastic-scales. |
| `telemetry` | `object({ sink, retention_days })` | `msk`-only: where the broker logs land. `otel` (the default) ships them to an S3 bucket this body creates, for DFE's own OTel feed; `cloudwatch` is the opt-in AWS-native path. Neither SaaS body emits a CloudWatch-shaped broker log, so neither takes this input. |

## Outputs

| Output | Type | Meaning |
|--------|------|---------|
| `bootstrap` | `string` | NORMALISED to bare `host:port`, comma-separated. MSK, Confluent and Redpanda each return this differently -- with a scheme, with a suffix, as a list -- and every DFE reader downstream wants one spelling. |
| `bootstrap_iam` | `string` | The IAM-authenticated endpoint, where the provider has one. The bootstrap Job's path; empty on a body with no such mechanism. |
| `auth_type` | `string` | `scram`, `plain` or `iam`. The chart's `external.auth.type` takes it unchanged. |
| `credential_ref` | `string` | A REFERENCE into the secrets store, never a value. |
| `network_attachment` | `object({ security_group_id, subnet_ids })` | The cloud-side half of the private handshake, so the caller can route to it and a later module can reach it. |
| `cluster_arn` | `string` | The vendor's handle on the cluster, for the alarms and policies a caller adds outside this module. |
| `bootstrap_role_arn` | `string` | The identity the in-cluster bootstrap Job assumes. The chart renders the Job; the module mints what it runs as. |
| `broker_log_bucket` | `string` | `msk`-only. The S3 bucket broker logs land in under `telemetry.sink = otel`. Empty under `cloudwatch`, and empty on both SaaS bodies. |

No `zookeeper` output. Every supported line is KRaft, and a body that returned
one would be describing a cluster we do not build.

## Rules every body follows

- **Private by default.** `connectivity = "private"` is the default and a body
  refuses to create a publicly reachable broker without `public` being asked
  for explicitly.
- **No credential in an output value.** `credential_ref` names a secret; the
  secret itself lives in the store.
- **The bootstrap string is bare.** No scheme, no trailing comma, no list --
  `host:port[,host:port]`.
- **The tuning block is data.** A setting the implementation manages or refuses
  is reported, not silently dropped.
- **No provider block.** A body declares `required_providers` and nothing else;
  the root configures the provider.
- **Nothing hardcoded.** The only literals allowed are ones the vendor's own API
  defines: managed policy names, service principals, and the port numbers the
  protocol fixes. Each carries a one-line comment saying why it is structural.

## What `msk/` builds

Express brokers, not Standard. MSK cannot convert a Standard cluster to Express
afterwards -- it has to be rebuilt -- so choosing Standard now to move later IS
the rebuild the choice is trying to avoid. Express is ARM only, manages its own
storage (no `storage_info`, and nothing here sizes a volume), and holds most of
the canonical profile read-only: the thread pools, the replication factor, the
segment size and unclean leader election are all refused. What reaches the
`aws_msk_configuration` is `num.partitions`, `log.retention.ms`,
`auto.create.topics.enable=false` and the 16 MiB size chain
(`message.max.bytes` and `replica.fetch.max.bytes`), which Express permits in
full. `log.retention.bytes` is in the Express set but has no derivation here:
it is sized from a PVC on the Strimzi path, and Express has no volume to bound.

Authentication is BOTH SASL/SCRAM and SASL/IAM. DFE's apps stay on SCRAM and
need no change. IAM exists for the in-cluster bootstrap Job alone: Kafka ACLs
live in the data plane, so tofu cannot write them, and once the cluster stops
granting by default nothing can create the FIRST ACL unless it is already
permitted. Under SASL/IAM the IAM policy is the authorisation and no ACL has to
exist, which is the way in. The Job -- the ACLs and the landing topics -- is
chart work; this module outputs the role it runs as.

Three MSK-specific traps the body honours, and one that does not apply:

- The SCRAM secret's name must start with `AmazonMSK_` and it must be encrypted
  with a customer-managed key. MSK refuses both otherwise, and reports it as an
  association failure rather than a create failure.
- The secret CONTAINER is created before the cluster and its VERSION after it.
- `lifecycle.ignore_changes` on `configuration_info` and `client_authentication`,
  because MSK rewrites both out of band and tofu would fight it on every plan.
  A deliberate change to either is landed by removing the ignore for that apply.
- `ebs_storage_info` is dfe-core's third ignore and has nothing to name on
  Express, which carries no EBS storage at all.

Intelligent rebalancing is declared `ACTIVE` rather than left to the API
default, so a broker added later has the existing partitions moved onto it.
Enabling it forecloses Cruise Control on the same cluster -- one rebalancer per
cluster, and on MSK it is MSK's.

**Broker logs follow `telemetry.sink`**, never both destinations at once.
`otel` (the default) delivers them to an S3 bucket this body creates --
lifecycle-expired at `telemetry.retention_days`, SSE-KMS on the deployment's own
key, public access blocked, `force_destroy` true because it is log spill and
not data DFE keeps. Because the log delivery service authenticates against a
resource policy rather than an IAM identity, this body ALSO extends the
deployment key's policy (`aws_kms_key_policy`, which replaces the whole policy)
to grant it -- something an IAM role policy cannot do for a service principal.
`cloudwatch` keeps the CloudWatch log group at `telemetry.retention_days`
instead and builds no bucket. `open_monitoring` (JMX + node exporter) stays on
under both -- it is scraped in-cluster, not delivered through either sink.

## What both SaaS bodies share

The vendor runs the brokers, so neither body sizes, tunes or versions them.
`broker_shape_ref`, `resolved_shapes`, `broker_count`, `kafka_version`,
`kms_key_arn`, `eks_cluster_name`, `pod_identity` and
`pod_identity_trust_policy_json` are declared and NOT APPLIED; each variable's
description names the vendor mechanism that replaces it. What survives of the
canonical profile is the per-topic half -- `num_partitions`, `log_retention_ms`
and `message_max_bytes` reach every landing topic.

Both add a `cluster_id` output, the vendor's own handle. `msk/` needs none: its
handle IS an ARN and `cluster_arn` carries it.

Both return empty for `bootstrap_iam` and `bootstrap_role_arn`. There is no
in-cluster bootstrap Job on either path -- the vendor's provider writes the
ACLs and the topics itself. That is also why the data-plane resources reach the
vendor's own API, and on private connectivity that API is private: **tofu itself
has to run inside the VPC**, or behind a resolver rule that forwards the
cluster's domain into it.

## What `redpanda-cloud/` builds

Serverless, the only tier the organisation can create. Dedicated and BYOC are
Request-access and neither publishes a rate card, so `tier` accepts all three
and REFUSES the two quote-only ones at plan with the reason. Their shape is
`redpanda_network` plus `redpanda_cluster` with `cluster_type` set to the tier.

`connectivity = "private"` is the default and builds a real attachment.
`redpanda_serverless_private_link` publishes an AWS VPC endpoint service and
allows this account's root principal, read from the caller rather than written
down; the `aws` provider creates the interface endpoint in the private subnets.
`private_dns_enabled` is on, which is what resolves the seed brokers inside the
VPC with no hosted zone of ours. Another VPC or an on-prem network resolves the
cluster domain through a Route 53 Resolver inbound endpoint and a forwarding
rule -- never by pointing a rule at the VPC's own `.2` resolver.

SASL/SCRAM-SHA-512, matching on-prem. The password reaches Redpanda as a
WRITE-ONLY argument so it never lands in state, which is why this body's floor
is OpenTofu 1.11 and why `scram_password_version` exists: a write-only value
cannot be compared between plan and apply.

`allow_deletion` defaults to TRUE. Every Redpanda resource defaults it false,
which makes `tofu destroy` refuse. A customer deployment sets it false.

## What `confluent-cloud/` builds

**SASL PLAIN over TLS, never SCRAM, on every tier.** `auth_type` is `plain` and
the credential is an API key and secret, the key being the username. DFE derives
the mechanism from the provider name, so this costs no code change -- but any
note saying Confluent does SCRAM is wrong.

`tier` is `freight` (the default), `enterprise` or `basic`. Freight has no
public endpoint and Basic has no private networking, so `connectivity` refuses
`freight` + `public` and `basic` + `private` at plan. Dedicated is not offered:
provisioned CKUs with no published rate.

The two private tiers take DIFFERENT handshakes, which is the trap:

- **Freight is a Private Network Interface, not PrivateLink.** Confluent's
  brokers attach to network interfaces in OUR VPC. The body creates them, grants
  Confluent's own AWS account `INSTANCE-ATTACH` on each -- that account ID read
  from `confluent_gateway`, never written down -- and hands the IDs to a
  `confluent_access_point`. Confluent documents 17 per subnet, 51 across three
  zones. `network_interfaces` is a SET with a six-interface floor, so the IDs
  must be distinct.
- **Enterprise is PrivateLink.** `confluent_private_link_attachment` publishes
  the endpoint service, the `aws` provider creates the endpoint, and
  `confluent_private_link_attachment_connection` names it back to Confluent. The
  service publishes no verified private DNS name, so `private_dns_enabled` is
  off and a private hosted zone serves the cluster's domain -- a wildcard record
  plus a zonal record per zone, which keeps a client's traffic in its own zone.

`private_network_interface_ids` takes interfaces the caller already created and
already permitted. Empty is the normal path. It is the way out if the access
point refuses interface IDs still unknown at plan, splitting the Freight
attachment across two applies; the contract test uses it because a mocked
provider cannot give 51 instances 51 distinct IDs.

TWO service accounts, for the reason MSK enables both SASL/IAM and SASL/SCRAM:
something must be allowed to write the first ACL, and it must not be the
credential DFE runs as. The manager account holds `CloudClusterAdmin` and is
tofu's alone; the DFE account holds the on-prem grant set and nothing more.
Confluent's principal is the service account's `sa-` ID, not its display name.

**Confluent caps a topic's `max.message.bytes` at 8 MiB**, under the 16 MiB the
rest of the size chain carries. The body applies what it is given rather than
clamping it -- a setting quietly halved here is worse than one the vendor
rejects out loud -- and a `check` block says so at plan.
