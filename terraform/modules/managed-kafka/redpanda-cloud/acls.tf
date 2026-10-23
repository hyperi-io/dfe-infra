// The SCRAM principal and its grants. dfe-engine creates every topic at
// startup, under the topic Create grant below, so this body creates none.
//
// One principal for the whole DFE fleet, at the grant set
// helm/charts/kafka/templates/kafka-user.yaml gives on-prem: topic * with Read,
// Write, Create, Describe, Delete, Alter, DescribeConfigs and AlterConfigs,
// which is every topic call dfe-engine makes, and consumer groups fenced to the
// dfe- prefix with Read and Describe. No cluster operations -- widening the
// grant past on-prem would make the cloud path looser than the one we run
// ourselves.
//
// These are data-plane resources, so on private connectivity they are written
// over the endpoint and tofu has to be able to reach it.

locals {
  // Redpanda's own spelling of the mechanism. SCRAM-SHA-512 rather than the
  // provider's 256 default, matching the on-prem user.
  scram_mechanism = "scram-sha-512"

  topic_grants = toset(["READ", "WRITE", "CREATE", "DESCRIBE", "DELETE", "ALTER", "DESCRIBE_CONFIGS", "ALTER_CONFIGS"])

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
