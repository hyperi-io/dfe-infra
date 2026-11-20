// Confluent Cloud over Confluent's private networking, Freight unless the dial names another tier.
// SASL_SSL with PLAIN: Confluent has no SCRAM, so auth_type is plain.
// Confluent runs the brokers, so nothing here sizes or tunes them. Why, per tier: docs/deployment/kafka/confluent-cloud.md#module-design-notes.

locals {
  private = var.connectivity == "private"
}

// Read only for the private handshake, so a Basic deploy needs no AWS credentials.
data "aws_caller_identity" "current" {
  count = local.private ? 1 : 0
}

data "aws_region" "current" {
  count = local.private ? 1 : 0
}

data "aws_availability_zone" "this" {
  for_each = local.private ? toset(var.network.azs) : toset([])

  name = each.value
}

locals {
  // Confluent's token for the deployment's cloud. var.cloud refuses all but AWS.
  cloud = var.cloud

  // The cluster builds on every tier, and Basic has no VPC to read the region from, so the caller names it.
  region = local.private ? data.aws_region.current[0].region : var.region

  // Confluent's enum: the multi-zone tiers take HIGH.
  availability = var.tier == "basic" ? "SINGLE_ZONE" : "HIGH"

  // The gateway is placed by zone ID, because the same zone has a different name in each account.
  zone_ids = local.private ? [for az in var.network.azs : data.aws_availability_zone.this[az].zone_id] : []

  subnet_by_az = zipmap(var.network.azs, var.network.private_subnet_ids)

  // Confluent's per-tier ceiling on a topic's max.message.bytes (figures in docs/deployment/kafka/confluent-cloud.md), checked below and never clamped.
  confluent_max_message_bytes = {
    basic      = 8388608
    enterprise = 20971520
    freight    = 20971520
  }[var.tier]
}

check "message_size_within_confluent_ceiling" {
  assert {
    condition     = var.message_max_bytes <= local.confluent_max_message_bytes
    error_message = "message_max_bytes is ${var.message_max_bytes}, above the ${local.confluent_max_message_bytes} Confluent Cloud allows for a topic's max.message.bytes on a ${var.tier} cluster (8388608 on Basic and Standard, 20971520 on Enterprise, Dedicated and Freight). Confluent will refuse the topics dfe-engine creates at startup. Lower the dial's message size, move to a tier with the higher ceiling, or put the deployment on a Kafka we run."
  }
}

// A check, not a validation: var.region has no neutral default.
check "region_named_on_public_path" {
  assert {
    condition     = local.private || var.region != ""
    error_message = "connectivity is public and var.region is empty -- there is no VPC to read the AWS region from on this path, so name it explicitly or Confluent will refuse the cluster."
  }
}

resource "confluent_environment" "this" {
  count = var.environment_id == "" ? 1 : 0

  display_name = "${var.name}-${var.env}"

  stream_governance {
    package = var.stream_governance_package
  }
}

// Keeps an existing deployment's environment from planning as a replacement.
moved {
  from = confluent_environment.this
  to   = confluent_environment.this[0]
}

locals {
  environment_id = var.environment_id != "" ? var.environment_id : confluent_environment.this[0].id
}

resource "confluent_kafka_cluster" "this" {
  display_name = var.name
  availability = local.availability
  cloud        = local.cloud
  region       = local.region

  // The provider refuses more than one tier block.
  dynamic "freight" {
    for_each = var.tier == "freight" ? [1] : []

    content {}
  }

  dynamic "enterprise" {
    for_each = var.tier == "enterprise" ? [1] : []

    content {}
  }

  dynamic "basic" {
    for_each = var.tier == "basic" ? [1] : []

    content {}
  }

  environment {
    id = local.environment_id
  }

  // The access point has to exist before the cluster is placed against it.
  depends_on = [confluent_access_point.pni]
}

// Confluent's REST ingress resets TLS for a cluster it has no route to yet, so the ACLs wait for the manager key to list topics (var.rest_ready).
// Public only: the deployer cannot reach a private endpoint.
resource "terraform_data" "rest_ready" {
  count = local.private ? 0 : 1

  triggers_replace = [confluent_kafka_cluster.this.id, confluent_api_key.manager.id]

  input = {
    url = "${confluent_kafka_cluster.this.rest_endpoint}/kafka/v3/clusters/${confluent_kafka_cluster.this.id}/topics"
  }

  // Through bash, so a checkout that dropped the mode bit still runs.
  provisioner "local-exec" {
    command = <<-EOT
      bash "${path.module}/scripts/wait-rest-ready.sh" "${self.output.url}" ${var.rest_ready.successes} ${var.rest_ready.timeout_seconds} ${var.rest_ready.interval_seconds}
    EOT

    // In the environment, not the command line, so ps never shows the key.
    environment = {
      CONFLUENT_REST_KEY    = confluent_api_key.manager.id
      CONFLUENT_REST_SECRET = confluent_api_key.manager.secret
    }
  }
}
