// The SCRAM principal, its grants and the landing topics.
//
// One principal for the whole DFE fleet, at the grant set kafka-user.yaml
// already gives on-prem: topic * with Read, Write, Create and Describe, and
// consumer groups fenced to the dfe- prefix with Read and Describe. No Delete,
// no Alter, no cluster operations -- widening the grant here would make the
// cloud path looser than the one we run ourselves.
//
// These are data-plane resources, so on private connectivity they are written
// over the endpoint and tofu has to be able to reach it.

locals {
  // Redpanda's own spelling of the mechanism. SCRAM-SHA-512 rather than the
  // provider's 256 default, matching the on-prem user.
  scram_mechanism = "scram-sha-512"

  topic_grants = toset(["READ", "WRITE", "CREATE", "DESCRIBE"])

  group_grants = toset(["READ", "DESCRIBE"])
}

resource "redpanda_user" "this" {
  name            = var.scram_username
  mechanism       = local.scram_mechanism
  cluster_api_url = local.cluster_api_url

  // Write-only: the password reaches Redpanda and is never stored in state.
  // The version is what tells the provider the value moved, because a
  // write-only argument cannot be compared between plan and apply.
  password_wo         = var.scram_password
  password_wo_version = var.scram_password_version

  allow_deletion = var.allow_deletion
}

resource "redpanda_acl" "topics" {
  for_each = local.topic_grants

  resource_type = "TOPIC"

  // The on-prem grant is topic * LITERAL, not a prefix: DFE's topic names are
  // set by the deployment and a prefix rule would have to guess them.
  resource_name         = "*"
  resource_pattern_type = "LITERAL"

  principal       = "User:${redpanda_user.this.name}"
  host            = "*"
  operation       = each.value
  permission_type = "ALLOW"
  cluster_api_url = local.cluster_api_url

  allow_deletion = var.allow_deletion
}

resource "redpanda_acl" "groups" {
  for_each = local.group_grants

  resource_type         = "GROUP"
  resource_name         = var.consumer_group_prefix
  resource_pattern_type = "PREFIXED"

  principal       = "User:${redpanda_user.this.name}"
  host            = "*"
  operation       = each.value
  permission_type = "ALLOW"
  cluster_api_url = local.cluster_api_url

  allow_deletion = var.allow_deletion
}

// Pre-created, never conjured by the first produce: dfe-loader treats a missing
// landing topic as fatal, and auto.create.topics.enable is not a setting
// Redpanda Cloud accepts on any tier -- so the partition count and retention
// are ours to set only if we create the topic.
resource "redpanda_topic" "landing" {
  for_each = var.landing_topics

  name            = each.key
  partition_count = coalesce(each.value.partitions, var.num_partitions)
  cluster_api_url = local.cluster_api_url

  // Null takes the cluster's own replication factor, which on Serverless is
  // the vendor's to choose.
  replication_factor = null

  configuration = {
    "cleanup.policy"    = "delete"
    "retention.ms"      = tostring(coalesce(each.value.retention_ms, var.log_retention_ms))
    "max.message.bytes" = tostring(var.message_max_bytes)
  }

  allow_deletion = var.allow_deletion
}
