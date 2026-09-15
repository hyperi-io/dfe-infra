// The aws root is thin by design: one provider, one backend, a rendered tfvars
// file and the module calls. Everything that creates a resource lives in a
// capability module, so gcp and azure are new roots over new bodies rather than
// a fork of this file.

data "aws_caller_identity" "current" {}

// data.aws_partition.current is declared in cloudtrail.tf and reused below in
// the KMS key policy grants' condition ARNs -- a CloudTrail ARN's partition
// varies the same way any other ARN's does. (The toolbox operator's EKS
// access-entry policy ARN uses its own copy, kubernetes-cluster/aws/eks.tf.)

locals {
  project = "dfe"
  cloud   = "aws"

  // Deletion protection follows the dial's tags.lifecycle, not a fixed answer:
  // ephemeral (tyre-kick, provision-test-destroy) deployments are torn down and
  // rebuilt under the same names, so they get the fastest teardown each service
  // allows; anything else -- persistent today, and any future value -- keeps
  // the vendor's own default so destroying a real deployment needs a
  // deliberate confirmation. (Named "lifecycle" is a reserved OpenTofu
  // variable identifier, so this reads tags.lifecycle rather than a var of
  // its own -- tags is where render_dial.py already renders it.)
  ephemeral                         = var.tags.lifecycle == "ephemeral"
  secret_recovery_window_days       = local.ephemeral ? 0 : 30
  kafka_secret_recovery_window_days = local.ephemeral ? 0 : 30

  // Every service principal the deployment's KMS key must grant, beyond the
  // account root -- collected here because the cluster module is the key's
  // ONE policy owner (aws_kms_key_policy replaces the whole policy) and
  // cloudtrail.tf's bucket and msk's broker-log delivery are both this root's
  // to know about. CloudTrail is unconditional: cloudtrail.tf's bucket is
  // SSE-KMS on this key regardless of which way telemetry.sink points. The
  // MSK grant is conditional on the otel sink actually creating a log-
  // delivery target -- confluent-cloud and redpanda-cloud emit no
  // CloudWatch-shaped broker log for anything to deliver.
  key_policy_grants = concat(
    [
      // AWS's documented CloudTrail-to-KMS statements: GenerateDataKey* needs
      // both the trail-ARN and EncryptionContext conditions, Decrypt/
      // DescribeKey need only the trail ARN. See
      // https://docs.aws.amazon.com/awscloudtrail/latest/userguide/create-kms-key-policy-for-cloudtrail.html
      {
        sid        = "AllowCloudTrailToGenerateDataKeys"
        principals = ["cloudtrail.amazonaws.com"]
        actions    = ["kms:GenerateDataKey*"]
        conditions = [
          { test = "StringEquals", variable = "aws:SourceArn", values = [local.cloudtrail_arn] },
          {
            test     = "StringLike"
            variable = "kms:EncryptionContext:aws:cloudtrail:arn"
            values   = ["arn:${data.aws_partition.current.partition}:cloudtrail:*:${data.aws_caller_identity.current.account_id}:trail/*"]
          },
        ]
      },
      {
        sid        = "AllowCloudTrailToReadTheKey"
        principals = ["cloudtrail.amazonaws.com"]
        actions    = ["kms:Decrypt", "kms:DescribeKey"]
        conditions = [
          { test = "StringEquals", variable = "aws:SourceArn", values = [local.cloudtrail_arn] },
        ]
      },
    ],
    var.kafka.provider == "msk" && var.telemetry.sink == "otel" ? [
      {
        sid        = "AllowMSKBrokerLogDeliveryToUseTheKey"
        principals = ["delivery.logs.amazonaws.com"]
        actions    = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
        conditions = []
      },
    ] : []
  )
}

// An SCP fences the test account to one region; nothing fences a deployment to
// the right ACCOUNT, and GCP and Azure have no SCP at all. So the assertion is
// in the config, where all three clouds can carry it.
resource "terraform_data" "account_guard" {
  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.provision.account
      error_message = "This shell is authenticated to AWS account ${data.aws_caller_identity.current.account_id}, but provision.account is ${var.provision.account}. Re-authenticate, or correct the dial."
    }
  }
}

module "naming" {
  source = "../../modules/tf-naming"

  project   = local.project
  component = "cluster"
  env       = var.env
  cloud     = local.cloud
  region    = var.provision.region
}

module "cluster" {
  source = "../../modules/kubernetes-cluster/aws"

  provision = {
    account = var.provision.account
    region  = var.provision.region
    cidr    = var.provision.cidr
  }

  name               = var.name
  env                = var.env
  kubernetes_version = var.kubernetes_version
  node_pools         = var.node_pools
  resolved_shapes    = var.resolved_shapes
  network            = var.network
  endpoint           = var.endpoint
  dns                = { private_zone = var.dns.private_zone }
  telemetry          = var.telemetry
  tags               = var.tags

  key_policy_grants = local.key_policy_grants

  // Empty unless the toolbox is enabled -- var.toolbox.aws.operator_role_arn
  // is required-when-enabled (deployment.example.yaml), and the module
  // itself treats an empty string as "create no access entry".
  toolbox_operator_role_arn = var.toolbox.enabled ? var.toolbox.aws.operator_role_arn : ""
}

// ---------------------------------------------------------------------------
// Edge -- every resource that exists because traffic crosses the VPC boundary:
// the load balancer controller's identity, the public zone, and the two
// controller identities that write it. `count` on the module block is the
// whole-module switch, so a disabled edge is an absent module rather than one
// full of zero-count resources, and every moved {} block in moved.tf therefore
// maps one instance to one instance.
// ---------------------------------------------------------------------------

module "edge" {
  count = var.edge.enabled ? 1 : 0

  source = "../../modules/edge/aws"

  name = var.name
  env  = var.env

  cluster_name = module.cluster.cluster_name

  network = {
    vpc_id             = module.cluster.network.vpc_id
    cidr               = module.cluster.network.cidr
    azs                = module.cluster.network.azs
    private_subnet_ids = module.cluster.network.private_subnet_ids
  }

  pod_identity_trust_policy_json = module.cluster.pod_identity_trust_policy_json
  private_zone_arn               = module.cluster.private_zone_arn
  kms_key_arn                    = module.cluster.kms_key_arn

  dns    = { public_zone = var.dns.public_zone }
  tunnel = var.edge.tunnel

  tags = var.tags
}

// The one Kafka password of the deployment, generated HERE rather than in the
// secrets module because a managed broker is CREATED with it: the same value has
// to reach the store ESO reads and the secret MSK authenticates against, and a
// value generated inside that module never leaves it -- which is what keeps a
// credential out of the plan and out of every output.
//
// special = false for the reason that module states: the value rides a JAAS
// string, where punctuation needs escaping and buys no entropy at 32 characters.
resource "random_password" "kafka_scram" {
  length  = 32
  special = false

  lifecycle {
    // NEVER regenerated. The brokers keep the password they were created with,
    // so a new one locks every DFE service out of Kafka. Rotation is a
    // deliberate, separate operation.
    ignore_changes = all
  }
}

// Nothing is created here unless the deployment asked for the managed broker.
// strimzi and redpanda run inside the cluster, and a SaaS broker is another body
// under managed-kafka rather than another branch in this root.
module "kafka" {
  count = var.kafka.provider == "msk" ? 1 : 0

  source = "../../modules/managed-kafka/msk"

  name = var.name
  env  = var.env

  network          = module.cluster.network
  eks_cluster_name = module.cluster.cluster_name
  kms_key_arn      = module.cluster.kms_key_arn

  broker_shape_ref = var.kafka.msk.shape_ref
  resolved_shapes  = var.resolved_shapes
  broker_count     = var.kafka.msk.broker_count
  kafka_version    = var.kafka.msk.broker_version

  num_partitions    = var.kafka.msk.num_partitions
  log_retention_ms  = var.kafka.msk.log_retention_ms
  message_max_bytes = var.kafka.msk.message_max_bytes

  secret_recovery_window_days = local.kafka_secret_recovery_window_days

  scram_username = var.kafka.msk.scram_username
  scram_password = random_password.kafka_scram.result

  pod_identity = {
    namespace       = var.kafka.msk.bootstrap_job.namespace
    service_account = var.kafka.msk.bootstrap_job.service_account
  }

  pod_identity_trust_policy_json = module.cluster.pod_identity_trust_policy_json

  autoscaling = var.kafka.msk.autoscaling
  telemetry   = var.telemetry

  // NO module-wide depends_on. The grant ordering this module needs now rides
  // on kms_key_arn itself, which the cluster module declares against its own
  // key POLICY; every other input above is an explicit reference that orders
  // against exactly the resource it names. A depends_on here would instead
  // hold the longest resource in the deployment behind the control plane, the
  // node groups and every addon, none of which a broker reads.
}

// Confluent Cloud sizes, tunes and versions the cluster itself (CONTRACT.md),
// so this root passes only what no vendor default can stand in for: the name,
// the network the private attachment lands in, and nothing else. The vendor API
// credential is NOT a tfvar -- `provider "confluent"` below reads
// CONFLUENT_CLOUD_API_KEY / CONFLUENT_CLOUD_API_SECRET from the deployer's own
// environment, the way the aws provider already reads AWS_* rather than a var.
module "confluent" {
  count = var.kafka.provider == "confluent-cloud" ? 1 : 0

  source = "../../modules/managed-kafka/confluent-cloud"

  name    = var.name
  env     = var.env
  network = module.cluster.network

  // The same canonical tuning msk applies, and the same landing topics its
  // own bootstrap Job would create -- this body has none, so tofu creates
  // them itself (CONTRACT.md).
  num_partitions    = var.kafka.num_partitions
  log_retention_ms  = var.kafka.log_retention_ms
  message_max_bytes = var.kafka.message_max_bytes
  landing_topics    = var.kafka.landing_topics
}

// Same reasoning as confluent-cloud: Redpanda Cloud Serverless sizes and scales
// itself, so only the name and network travel. The SCRAM password reuses the
// SAME seed msk is created with -- one Kafka password per deployment, whichever
// provider ends up authenticating against it -- and the vendor client
// credential is a provider environment variable (REDPANDA_CLIENT_ID /
// REDPANDA_CLIENT_SECRET), never a tfvar.
module "redpanda" {
  count = var.kafka.provider == "redpanda-cloud" ? 1 : 0

  source = "../../modules/managed-kafka/redpanda-cloud"

  name    = var.name
  env     = var.env
  network = module.cluster.network

  scram_password = random_password.kafka_scram.result

  // The same canonical tuning msk applies, and the same landing topics its
  // own bootstrap Job would create -- this body has none, so tofu creates
  // them itself (CONTRACT.md).
  num_partitions    = var.kafka.num_partitions
  log_retention_ms  = var.kafka.log_retention_ms
  message_max_bytes = var.kafka.message_max_bytes
  landing_topics    = var.kafka.landing_topics

  // The provider defaults this true, which makes tofu destroy refuse. Only an
  // ephemeral deployment gets that: CONTRACT.md's own words are "a customer
  // deployment sets it false", and lifecycle is what tells the two apart.
  allow_deletion = local.ephemeral
}

locals {
  // The managed broker's handles, or empty strings when the deployment runs its
  // own brokers or a different SaaS body. var.kafka.provider selects at most
  // ONE of the three modules, so concatenating every body's output the same way
  // and joining over "at most one populated list" is coalescing, not an
  // ambiguity between vendors -- and the outputs below need no index that may
  // not exist.
  managed_kafka = {
    bootstrap = join("", concat(
      module.kafka[*].bootstrap,
      module.confluent[*].bootstrap,
      module.redpanda[*].bootstrap,
    ))
    // one(), not join(): the port is a number, not a string, and var.kafka.
    // provider selects at most one body, so the concat is 0 or 1 elements --
    // one() reads that single element, or returns null when no body is
    // selected at all.
    bootstrap_port = one(concat(
      module.kafka[*].bootstrap_port,
      module.confluent[*].bootstrap_port,
      module.redpanda[*].bootstrap_port,
    ))
    bootstrap_iam = join("", concat(
      module.kafka[*].bootstrap_iam,
      module.confluent[*].bootstrap_iam,
      module.redpanda[*].bootstrap_iam,
    ))
    credential_ref = join("", concat(
      module.kafka[*].credential_ref,
      module.confluent[*].credential_ref,
      module.redpanda[*].credential_ref,
    ))
    bootstrap_role_arn = join("", concat(
      module.kafka[*].bootstrap_role_arn,
      module.confluent[*].bootstrap_role_arn,
      module.redpanda[*].bootstrap_role_arn,
    ))
  }
}

module "secrets" {
  source = "../../modules/secrets/aws-sm"

  project = local.project
  env     = var.env
  prefix  = var.secrets.ref
  seeds   = var.seeds

  // The same value a managed broker was created with, so the store and Kafka
  // cannot disagree. An in-cluster broker reads it from the store too, through
  // the ExternalSecret the kafka chart renders.
  kafka_password = random_password.kafka_scram.result

  kms_key_arn                    = module.cluster.kms_key_arn
  cluster_name                   = module.cluster.cluster_name
  pod_identity_trust_policy_json = module.cluster.pod_identity_trust_policy_json

  recovery_window_days = local.secret_recovery_window_days
}

// ---------------------------------------------------------------------------
// Toolbox -- the on-demand SSM-managed troubleshooting instance. This module
// is ALWAYS instantiated (no `count` on the block): `var.toolbox.enabled` is
// threaded through as a plain input, because toolbox/aws's own session-log
// bucket must survive an up/down cycle rather than being destroyed and
// recreated with everything else -- see that module's CONTRACT.md.
// ---------------------------------------------------------------------------

module "toolbox_naming" {
  source = "../../modules/tf-naming"

  project   = local.project
  component = "toolbox"
  env       = var.env
  cloud     = local.cloud
  region    = var.provision.region
}

locals {
  // The named forward targets `dfe-ops bastion forward` picks a Session
  // document by -- computed here from the OTHER modules' own outputs, never
  // invented in the toolbox module itself (toolbox/aws/CONTRACT.md).
  toolbox_eks_api_target = {
    eks-api = {
      // module.cluster.cluster_endpoint is a full URL (https://<host>); the
      // Port session document's `host` property takes a bare hostname.
      host = trimsuffix(replace(module.cluster.cluster_endpoint, "https://", ""), "/")
      port = 443
      // The private endpoint resolves to control-plane interfaces inside the
      // VPC, and 443 is served by the module's own control rule regardless.
      scope = "vpc"
    }
  }

  // for_each keys must be known at plan; only the VALUES may stay unknown
  // until apply. local.managed_kafka.bootstrap (the broker host) is unknown
  // until a managed broker actually exists, but the key here ("kafka") and
  // the port (managed-kafka/CONTRACT.md's bootstrap_port, a literal each
  // body's outputs.tf hardcodes) are both known from var.kafka.provider
  // alone -- which is what lets the toolbox be enabled on a fresh apply,
  // before MSK/Confluent/Redpanda has ever been created. A single target
  // reaches ONE bootstrap broker, which proves reachability, TLS and SASL;
  // a client that must follow Kafka's own metadata response to the other
  // brokers runs ON the instance itself (`dfe-ops bastion shell`), because the
  // brokers' advertised hostnames only resolve inside the VPC. That client is
  // the Apache Kafka console scripts, pinned to the deployment's own broker
  // version -- the instance's distribution publishes no kcat package, which
  // toolbox/aws/CONTRACT.md and docs/deployment/toolbox.md both record.
  managed_kafka_selected = contains(["msk", "confluent-cloud", "redpanda-cloud"], var.kafka.provider)
  // MSK's brokers are ENIs in this VPC; Confluent Cloud and Redpanda Cloud
  // publish a vendor-hosted bootstrap the instance reaches over NAT, so their
  // egress rule has to leave the VPC or the forward hangs.
  toolbox_kafka_scope = var.kafka.provider == "msk" ? "vpc" : "internet"
  toolbox_kafka_targets = local.managed_kafka_selected ? merge(
    {
      kafka = {
        host  = split(":", split(",", local.managed_kafka.bootstrap)[0])[0]
        port  = local.managed_kafka.bootstrap_port
        scope = local.toolbox_kafka_scope
      }
    },
    // MSK's SASL/IAM listener, a SECOND target rather than a variant of the
    // one above: it is a different port with different authentication, and
    // without its own entry nothing opens 9098 in the toolbox's egress. Gated
    // on the provider token, not on bootstrap_iam being non-empty, for the
    // plan-time reason stated above -- the endpoint is unknown until the
    // cluster exists, and a for_each key may not be. Confluent Cloud and
    // Redpanda Cloud publish no IAM listener at all.
    var.kafka.provider == "msk" ? {
      kafka-iam = {
        host  = split(":", split(",", local.managed_kafka.bootstrap_iam)[0])[0]
        port  = 9098
        scope = "vpc"
      }
    } : {},
  ) : {}

  // A Kubernetes Service name resolves through CoreDNS, inside the cluster,
  // and the instance sits outside it -- so advertising one as a forward target
  // names an address that can never resolve from the box advertising it. An
  // in-cluster ClickHouse is reached over the eks-api target instead, with
  // `kubectl port-forward` from the toolbox shell. A ClickHouse the deployer
  // brought (ClickHouse Cloud, a VM in the VPC) has a real address and keeps
  // its target. 9440 is the native protocol over TLS (helm/charts/
  // network-policies' own values.yaml documents this port for the same reason).
  // A dotless host counts too: `dfe-clickhouse` is a Service short name that
  // only resolves through a pod's search domains, and the instance has none.
  clickhouse_host_is_in_cluster = (
    endswith(var.endpoints.clickhouse_host, ".cluster.local")
    || endswith(var.endpoints.clickhouse_host, ".svc")
    || !strcontains(var.endpoints.clickhouse_host, ".")
  )
  // A name in the deployment's own private zone resolves to an address inside
  // the VPC; anything else the deployer supplied -- a ClickHouse Cloud host,
  // the `mode: external` case -- is reached over NAT, so its egress rule has
  // to leave the VPC.
  clickhouse_scope = endswith(var.endpoints.clickhouse_host, ".${var.dns.private_zone}") ? "vpc" : "internet"
  toolbox_clickhouse_target = var.endpoints.clickhouse_host != "" && !local.clickhouse_host_is_in_cluster ? {
    clickhouse = {
      host  = var.endpoints.clickhouse_host
      port  = 9440
      scope = local.clickhouse_scope
    }
  } : {}

  // No Keeper target: no root output or dial field names a Keeper address
  // today, and inventing a Service DNS name that might not match the actual
  // chart's naming is worse than omitting it -- see docs/deployment/toolbox.md.
  toolbox_targets = merge(
    local.toolbox_eks_api_target,
    local.toolbox_kafka_targets,
    local.toolbox_clickhouse_target,
  )

  // The reserved range every appliance's tunnel address is issued out of --
  // helm/edge/culvert/values.yaml vpn.clientCIDR, which no tofu input carries.
  // scripts/tests/test_tunnel_forwarder_facts.py holds the two in step.
  tunnel_client_cidr = "100.64.0.0/10"
}

module "toolbox" {
  source = "../../modules/toolbox/aws"

  name = module.toolbox_naming.canonical_name
  env  = var.env

  enabled = var.toolbox.enabled

  network = {
    vpc_id             = module.cluster.network.vpc_id
    cidr               = module.cluster.network.cidr
    private_subnet_ids = module.cluster.network.private_subnet_ids
  }

  instance_type = var.toolbox.aws.instance_type
  ttl_minutes   = var.toolbox.ttl_minutes
  tool_versions = var.toolbox.tool_versions

  session                    = var.toolbox.session
  session_log_retention_days = var.toolbox.session_log_retention_days

  kms_key_arn = module.cluster.kms_key_arn
  targets     = local.toolbox_targets

  // The fleet tunnel's address and listeners, so `dfe-ops bastion join` can
  // dial the hub as an admin peer. Both empty unless the edge module built a
  // forwarder, and an empty address renders no egress rule at all.
  //
  // client_cidr and reach are the OTHER half, and they do not need the address:
  // the reach-back that works today routes the client range at the culvert pod
  // rather than dialling in, so it is TCP to a tunnel address inside the VPC.
  // `join`/`flatten` rather than `one`, because `one([])` is null and `try` only
  // catches an error -- a disabled edge would hand this module a null address and
  // fail its own variable validation before any plan could be read.
  tunnel = {
    address     = join("", module.edge[*].tunnel_address)
    ports       = flatten(module.edge[*].tunnel_listener_ports)
    client_cidr = var.edge.tunnel.admin_peer.enabled ? local.tunnel_client_cidr : ""
    reach       = var.edge.tunnel.admin_peer.reach
  }

  // The one group the toolbox needs admitting to that it does not own -- the
  // module adds a single 443 ingress rule naming its own group, and takes it
  // away again on `bastion down`.
  eks_cluster_security_group_id = module.cluster.cluster_security_group_id

  // Follows tags.lifecycle the same way cloudtrail.tf's bucket does: an
  // ephemeral tyre-kick deployment is rebuilt under the same name and needs
  // the fast teardown; a persistent one keeps the safer default.
  force_destroy_session_logs = local.ephemeral

  tags = var.tags
}

// The toolbox operator's read-only EKS access entry (AmazonEKSViewPolicy)
// lives INSIDE the cluster module now (kubernetes-cluster/aws/eks.tf,
// "Toolbox operator"), fed by toolbox_operator_role_arn above -- not as a
// standalone resource here. The cluster module already owns the creator's
// cluster-admin entry and the Karpenter node entry; a third EKS access entry
// belongs beside them rather than duplicated against the same cluster from a
// second resource address, which is what this used to be.
