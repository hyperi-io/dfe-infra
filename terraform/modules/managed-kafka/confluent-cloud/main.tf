// Confluent Cloud, reached over Confluent's own private networking. Freight by
// default: it autoscales on eCKUs, is the cheapest option at the top band, and
// has no public endpoint at all, so it is egress-safe by construction.
//
// Confluent is SASL_SSL with PLAIN over an API key and secret. It does NOT do
// SCRAM on any tier -- scalo derives the mechanism from the provider name, so
// this costs no code change, but auth_type here is plain and never scram.
//
// The vendor runs the brokers, so nothing here sizes, tunes or versions them:
// the canonical broker profile applies to the Kafka we run, and what survives
// on this path is the per-topic half.

locals {
  private = var.connectivity == "private"
}

// Basic (public, no AWS attachment) reads no AWS fact at all -- these three
// exist only to place the private handshake, so a Basic deploy would
// otherwise demand AWS credentials it has no other use for. Gated on
// local.private rather than on the tier, because both private tiers
// (freight, enterprise) need them and only Basic does not.
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
  // Confluent's own token for the cloud this body's second provider talks to.
  // GCP and Azure get their own second provider when S9.T6 cuts them.
  cloud = "AWS"

  // confluent_kafka_cluster.this builds on EVERY tier, private or public, so
  // its region has to resolve even when data.aws_region is not read: on the
  // private path it is discovered from the VPC's own provider region: on
  // Basic there is no VPC to discover it from, so the caller names it.
  region = local.private ? data.aws_region.current[0].region : var.region

  // Freight and Enterprise are multi-zone products and take HIGH; Basic is the
  // single-zone one. Confluent's own enum, not a choice of ours.
  availability = var.tier == "basic" ? "SINGLE_ZONE" : "HIGH"

  // The gateway is placed by availability zone ID, not by zone name -- the same
  // zone carries a different name in each account. Empty on the public path,
  // where nothing reads it -- Basic builds no gateway.
  zone_ids = local.private ? [for az in var.network.azs : data.aws_availability_zone.this[az].zone_id] : []

  // One private subnet per zone, in the order the zones are listed.
  subnet_by_az = zipmap(var.network.azs, var.network.private_subnet_ids)

  // Confluent Cloud refuses a topic max.message.bytes above 8 MiB on every
  // tier this module offers. Applied as given rather than clamped -- a setting
  // this body quietly halved would be worse than one the vendor rejects out
  // loud -- with the check block below saying so at plan.
  confluent_max_message_bytes = 8388608
}

check "message_size_within_confluent_ceiling" {
  assert {
    condition     = var.message_max_bytes <= local.confluent_max_message_bytes
    error_message = "message_max_bytes is ${var.message_max_bytes}, above Confluent Cloud's 8388608 ceiling for a topic's max.message.bytes. The apply will be refused by Confluent. Lower the dial's message size for this deployment, or put it on a Kafka we run."
  }
}

// A soft warning, not a hard validation: var.region has no safe non-empty
// default (there is no vendor-neutral "right" region), and failing the plan
// outright here would be a worse surprise than Confluent's own refusal at
// apply for a cluster with no region.
check "region_named_on_public_path" {
  assert {
    condition     = local.private || var.region != ""
    error_message = "connectivity is public and var.region is empty -- there is no VPC to read the AWS region from on this path, so name it explicitly or Confluent will refuse the cluster."
  }
}

// ---------------------------------------------------------------------------
// The environment and the cluster
// ---------------------------------------------------------------------------

resource "confluent_environment" "this" {
  display_name = "${var.name}-${var.env}"

  stream_governance {
    package = var.stream_governance_package
  }
}

resource "confluent_kafka_cluster" "this" {
  display_name = var.name
  availability = local.availability
  cloud        = local.cloud
  region       = local.region

  // Exactly one tier block, and the provider refuses more than one. A dynamic
  // block per tier is how one body offers three without three resources.
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
    id = confluent_environment.this.id
  }

  // Freight reaches the deployment through the Private Network Interface, and
  // the access point has to exist before the cluster is placed against it.
  depends_on = [confluent_access_point.pni]
}
