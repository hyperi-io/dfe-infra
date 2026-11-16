// The aws root: one provider, one backend, a rendered tfvars file and the module calls.

data "aws_caller_identity" "current" {}

locals {
  project = "dfe"
  cloud   = "aws"

  // Deletion protection follows tags.lifecycle (docs/deployment/aws-teardown.md).
  // "lifecycle" is a reserved variable name, so this reads it off var.tags.
  ephemeral                         = var.tags.lifecycle == "ephemeral"
  secret_recovery_window_days       = local.ephemeral ? 0 : 30
  kafka_secret_recovery_window_days = local.ephemeral ? 0 : 30

  // The tags the controllers need explicitly, beyond default_tags.
  run_tags = merge(
    module.run_tags.tags,
    var.inspector_ec2_exclusion ? tomap({ InspectorEc2Exclusion = "true" }) : tomap({}),
  )

  tags = merge(var.tags, local.run_tags)

  // Grants beyond the account root, passed to the cluster module, which owns the key policy (docs/deployment/aws-configuration.md).
  key_policy_grants = concat(
    var.cloudtrail.enabled ? [
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
    ] : [],
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

// Fails the plan when the shell is authenticated to an account other than provision.account.
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

// No resources: renders var.run's two tags, empty when null.
module "run_tags" {
  source = "../../modules/tf-run-tags"

  run = var.run
}

// No resources: refuses an offering aws lacks (docs/deployment/managed-backends.md).
module "managed_backends" {
  source = "../../modules/managed-backends"

  cloud           = local.cloud
  kafka_provider  = var.kafka.provider
  kafka_tier      = var.kafka.provider == "confluent-cloud" ? var.kafka.confluent_tier : null
  clickhouse_mode = var.clickhouse.mode
  clickhouse_host = var.endpoints.clickhouse_host
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
  addon_versions     = var.eks_addon_versions
  node_pools         = var.node_pools
  resolved_shapes    = var.resolved_shapes
  node_image         = var.node_image
  network            = var.network
  endpoint           = var.endpoint
  dns                = { private_zone = var.dns.private_zone }
  telemetry          = var.telemetry
  tags               = local.tags

  // The run's tags alone, for what the EKS add-ons create where default_tags cannot reach.
  controller_tags      = local.run_tags
  permissions_boundary = var.permissions_boundary
  iam_path             = var.iam_path
  s3_bucket_prefix     = var.s3_bucket_prefix

  key_policy_grants = local.key_policy_grants

  // An empty string creates no access entry.
  toolbox_operator_role_arn = var.toolbox.enabled ? var.toolbox.aws.operator_role_arn : ""

  // The registry cache's containerd mirrors, on every managed node; Karpenter's take it through DFE_NODE_USER_DATA.
  node_user_data = local.node_user_data
}

// Registry cache (docs/deployment/aws-operations.md#registry-cache)

module "registry_cache" {
  count = var.registry_cache.enabled && length(var.registry_cache.upstreams) > 0 ? 1 : 0

  source = "../../modules/registry-cache/aws"

  provision = {
    account = var.provision.account
    region  = var.provision.region
  }

  name        = var.name
  namespace   = var.registry_cache.namespace != "" ? var.registry_cache.namespace : var.name
  upstreams   = var.registry_cache.upstreams
  expiry_days = var.registry_cache.expiry_days
  kms_key_arn = module.cluster.kms_key_arn

  node_role_arns = {
    managed   = module.cluster.node_role_arn
    karpenter = module.cluster.karpenter.node_role_arn
  }

  tags = local.tags

  permissions_boundary = var.permissions_boundary
  iam_path             = var.iam_path
}

locals {
  node_user_data = join("", module.registry_cache[*].node_user_data)
}

// Edge: everything that exists because traffic crosses the VPC boundary.

locals {
  // Read only on mode forwarder.
  forwarder_shape = var.edge.tunnel.address.mode == "forwarder" ? var.resolved_shapes[var.edge.tunnel.address.shape_ref] : null
}

module "edge" {
  count = var.edge.enabled ? 1 : 0

  source = "../../modules/edge/aws"

  name = var.name
  env  = var.env

  cluster_name = module.cluster.cluster_name

  // The public subnets, because the forwarder's Elastic IP needs an internet gateway route.
  network = {
    vpc_id            = module.cluster.network.vpc_id
    cidr              = module.cluster.network.cidr
    azs               = module.cluster.network.azs
    public_subnet_ids = module.cluster.network.public_subnet_ids
  }

  // Admits the forwarder's DNAT target at the node.
  node_security_group_id = module.cluster.cluster_security_group_id

  pod_identity_trust_policy_json = module.cluster.pod_identity_trust_policy_json
  private_zone_arn               = module.cluster.private_zone_arn
  kms_key_arn                    = module.cluster.kms_key_arn

  dns      = { public_zone = var.dns.public_zone, parent_zone_id = var.dns.parent_zone_id }
  receiver = var.edge.receiver

  // The dial's tunnel, with the forwarder's type and root volume read off its resolved shape.
  tunnel = {
    address = {
      mode          = var.edge.tunnel.address.mode
      zone          = var.edge.tunnel.address.zone
      instance_type = local.forwarder_shape == null ? null : local.forwarder_shape.instance_types[0]
      root_volume   = local.forwarder_shape == null ? null : local.forwarder_shape.root_volume
      ami_id        = local.forwarder_shape == null ? null : var.forwarder_ami_id
    }
    openvpn       = var.edge.tunnel.openvpn
    source_ranges = var.edge.tunnel.source_ranges
    node_ports    = var.edge.tunnel.node_ports
  }

  extra_self_ranges           = var.edge_self_ranges
  gateway_trusted_proxy_cidrs = var.edge_trusted_proxy_cidrs
  egress_addresses            = module.cluster.nat_public_ips

  permissions_boundary = var.permissions_boundary
  iam_path             = var.iam_path

  tags = local.tags
}

// The deployment's one Kafka password (docs/deployment/aws-configuration.md#kafka).
// special = false: the value rides JAAS strings, where punctuation needs escaping.
resource "random_password" "kafka_scram" {
  length  = 32
  special = false

  lifecycle {
    // Brokers keep the password they were created with, so a new one locks every DFE service out.
    ignore_changes = all
  }
}

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
  // The resolver's count wins. broker_count serves a hand-written shape with none.
  broker_count  = coalesce(try(var.resolved_shapes[var.kafka.msk.shape_ref].count, null), var.kafka.msk.broker_count)
  kafka_version = var.kafka.msk.broker_version

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

  // An empty string when the pin is absent, which the module's own validation refuses by name.
  broker_scaler_runtime = lookup(var.lambda_runtimes, "msk-broker-scaler", "")

  permissions_boundary = var.permissions_boundary
  iam_path             = var.iam_path
  s3_bucket_prefix     = var.s3_bucket_prefix

  // No module-wide depends_on: it would hold the broker behind the control plane, node groups and add-ons
  // (docs/deployment/aws-configuration.md, "What the apply builds").
}

// Confluent Cloud sizes and versions itself. Its API credential comes from the environment (providers.tf).
module "confluent" {
  count = var.kafka.provider == "confluent-cloud" ? 1 : 0

  source = "../../modules/managed-kafka/confluent-cloud"

  name    = var.name
  env     = var.env
  network = module.cluster.network

  // The vendor's token for aws.
  cloud = module.managed_backends.kafka.vendor_cloud

  // Read on public connectivity only.
  tier           = var.kafka.confluent_tier
  connectivity   = var.kafka.connectivity
  region         = var.provision.region
  environment_id = var.kafka.confluent_environment_id
  rest_ready     = var.kafka.confluent_rest_ready

  // Checked against the tier's per-topic ceiling, then handed to dfe-engine through DFE_KAFKA_MESSAGE_MAX_BYTES.
  message_max_bytes = var.kafka.message_max_bytes
}

// Redpanda Cloud Serverless sizes itself. It reuses the msk password seed, and its API credential comes from the environment.
module "redpanda" {
  count = var.kafka.provider == "redpanda-cloud" ? 1 : 0

  source = "../../modules/managed-kafka/redpanda-cloud"

  name    = var.name
  env     = var.env
  network = module.cluster.network

  // The vendor's token for aws.
  cloud = module.managed_backends.kafka.vendor_cloud

  tier              = var.kafka.redpanda_tier
  connectivity      = var.kafka.connectivity
  resource_group_id = var.kafka.redpanda_resource_group_id

  scram_password = random_password.kafka_scram.result

  // The provider defaults this true, which makes tofu destroy refuse.
  allow_deletion = local.ephemeral
}

locals {
  // The managed broker's handles, or empty when none is selected. var.kafka.provider selects at most one of the three modules.
  managed_kafka = {
    bootstrap = join("", concat(
      module.kafka[*].bootstrap,
      module.confluent[*].bootstrap,
      module.redpanda[*].bootstrap,
    ))
    // one(), not join(): the port is a number. Null when no body is selected.
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

  // The credential the broker authenticates, so the store and Kafka cannot disagree. MSK has no kafka seed: ESO reads its AmazonMSK_ secret.
  kafka_username   = var.kafka.provider == "confluent-cloud" ? one(module.confluent[*].dfe_api_key) : null
  kafka_password   = var.kafka.provider == "confluent-cloud" ? one(module.confluent[*].dfe_api_secret) : random_password.kafka_scram.result
  read_secret_arns = module.kafka[*].credential_ref

  // Seeded into clickhouse/external in external mode only.
  clickhouse_password = var.clickhouse_external_password

  kms_key_arn                    = module.cluster.kms_key_arn
  cluster_name                   = module.cluster.cluster_name
  pod_identity_trust_policy_json = module.cluster.pod_identity_trust_policy_json

  // The namespace DFE_NAMESPACE hands bootstrap.
  engine_namespace = module.naming.k8s_namespace

  recovery_window_days = local.secret_recovery_window_days

  permissions_boundary = var.permissions_boundary
  iam_path             = var.iam_path
}

// Toolbox (docs/deployment/toolbox.md). Always instantiated, with var.toolbox.enabled as an input, so
// bastion up and down never destroy the session-log bucket.

module "toolbox_naming" {
  source = "../../modules/tf-naming"

  project   = local.project
  component = "toolbox"
  env       = var.env
  cloud     = local.cloud
  region    = var.provision.region
}

locals {
  // The forward targets `dfe-ops bastion forward` picks a Session document by (docs/deployment/toolbox.md#forward-targets).
  toolbox_eks_api_target = {
    eks-api = {
      // The Port session document's `host` takes a bare hostname.
      host  = trimsuffix(replace(module.cluster.cluster_endpoint, "https://", ""), "/")
      port  = 443
      scope = "vpc"
    }
  }

  // for_each keys and the port are known at plan from var.kafka.provider, so the broker host may stay unknown until apply.
  managed_kafka_selected = contains(["msk", "confluent-cloud", "redpanda-cloud"], var.kafka.provider)
  // MSK fixes its SASL/IAM listener on this port.
  msk_iam_listener_port = 9098
  // Confluent Cloud and Redpanda Cloud are reached over NAT, so their egress rule has to leave the VPC.
  toolbox_kafka_scope = var.kafka.provider == "msk" ? "vpc" : "internet"
  toolbox_kafka_targets = local.managed_kafka_selected ? merge(
    {
      kafka = {
        host  = split(":", split(",", local.managed_kafka.bootstrap)[0])[0]
        port  = local.managed_kafka.bootstrap_port
        scope = local.toolbox_kafka_scope
      }
    },
    // A second target: another port, with its own egress entry. Gated on the provider token because bootstrap_iam is unknown at plan.
    var.kafka.provider == "msk" ? {
      kafka-iam = {
        host  = split(":", split(",", local.managed_kafka.bootstrap_iam)[0])[0]
        port  = local.msk_iam_listener_port
        scope = "vpc"
      }
    } : {},
  ) : {}

  // A Service name never resolves from the instance, so it is no forward target. A dotless host counts as one too.
  clickhouse_host_is_in_cluster = (
    endswith(var.endpoints.clickhouse_host, ".cluster.local")
    || endswith(var.endpoints.clickhouse_host, ".svc")
    || !strcontains(var.endpoints.clickhouse_host, ".")
  )
  // Outside the deployment's private zone the host is reached over NAT, so its egress rule has to leave the VPC.
  clickhouse_scope = endswith(var.endpoints.clickhouse_host, ".${var.dns.private_zone}") ? "vpc" : "internet"
  toolbox_clickhouse_target = var.endpoints.clickhouse_host != "" && !local.clickhouse_host_is_in_cluster ? {
    clickhouse = {
      host  = var.endpoints.clickhouse_host
      port  = var.clickhouse.native_port
      scope = local.clickhouse_scope
    }
  } : {}

  // Every port a client on the instance dials past the forwarded one, from managed-backends or the dial's own two for an external ClickHouse.
  toolbox_clickhouse_ports = (
    module.managed_backends.clickhouse.service != "" ? module.managed_backends.clickhouse.client_ports :
    var.clickhouse.mode == "external" ? [var.clickhouse.port, var.clickhouse.native_port] :
    []
  )
  toolbox_backend_egress = concat(
    [
      for port in concat(module.managed_backends.kafka.client_ports, module.managed_backends.kafka.bootstrap_ports) :
      { port = port, scope = local.toolbox_kafka_scope }
    ],
    [for port in local.toolbox_clickhouse_ports : { port = port, scope = local.clickhouse_scope }],
  )

  // No Keeper target: nothing names a Keeper address.
  toolbox_targets = merge(
    local.toolbox_eks_api_target,
    local.toolbox_kafka_targets,
    local.toolbox_clickhouse_target,
  )

  // Read whether or not the toolbox is enabled.
  toolbox_shape = var.resolved_shapes[var.toolbox.aws.shape_ref]
}

// Session transcripts, outside module.toolbox because `bastion down` destroys that module.
module "toolbox_session_logs" {
  source = "../../modules/s3-bucket"

  name        = "${var.s3_bucket_prefix}${module.toolbox_naming.canonical_name}-toolbox-session-logs"
  retain      = !local.ephemeral
  kms_key_arn = module.cluster.kms_key_arn
  versioning  = true

  lifecycle_rules = [
    {
      id                         = "expire-session-logs"
      expiration_days            = var.toolbox.session_log_retention_days
      noncurrent_expiration_days = var.toolbox.session_log_retention_days
    },
  ]

  tags = merge(local.tags, { Name = "${module.toolbox_naming.canonical_name}-toolbox-session-logs" })
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

  instance_type = local.toolbox_shape.instance_types[0]
  root_volume   = local.toolbox_shape.root_volume
  ami_id        = var.instance_ami_id
  ttl_minutes   = var.toolbox.ttl_minutes
  tool_versions = var.toolbox.tool_versions

  session                = var.toolbox.session
  session_log_bucket     = module.toolbox_session_logs.bucket
  session_log_bucket_arn = module.toolbox_session_logs.arn

  kms_key_arn    = module.cluster.kms_key_arn
  targets        = local.toolbox_targets
  backend_egress = local.toolbox_backend_egress

  // For `dfe-ops bastion join`. Empty unless the edge module built a forwarder, which renders no egress rule.
  // join/flatten rather than one(): a disabled edge would hand the module a null address and fail its validation.
  tunnel = {
    address     = join("", module.edge[*].tunnel_address)
    ports       = flatten(module.edge[*].tunnel_listener_ports)
    client_cidr = var.edge.tunnel.admin_peer.enabled ? var.edge.tunnel.client_cidr : ""
    reach       = var.edge.tunnel.admin_peer.reach
  }

  // The module adds one 443 ingress rule on this group and removes it on `bastion down`.
  eks_cluster_security_group_id = module.cluster.cluster_security_group_id

  permissions_boundary = var.permissions_boundary
  iam_path             = var.iam_path

  tags = local.tags
}
