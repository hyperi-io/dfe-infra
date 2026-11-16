// The SCRAM principal and the grants dfe-engine needs, the same set as helm/charts/kafka/templates/kafka-user.yaml. dfe-engine creates the topics.
// Private connectivity needs tofu to reach the endpoint. docs/deployment/kafka/redpanda-cloud.md#module-design-notes.

locals {
  // SCRAM-SHA-512 to match the on-prem user, where the provider defaults to 256.
  scram_mechanism = "scram-sha-512"

  topic_grants = toset(["READ", "WRITE", "CREATE", "DESCRIBE", "DELETE", "ALTER", "DESCRIBE_CONFIGS", "ALTER_CONFIGS"])

  group_grants = toset(["READ", "DESCRIBE"])
}

resource "redpanda_user" "this" {
  name            = var.scram_username
  mechanism       = local.scram_mechanism
  cluster_api_url = local.cluster_api_url

  // Write-only, so the password never reaches state. The version tells the provider it moved.
  password_wo         = var.scram_password
  password_wo_version = var.scram_password_version

  allow_deletion = var.allow_deletion
}

resource "redpanda_acl" "topics" {
  for_each = local.topic_grants

  resource_type = "TOPIC"

  // Topic * LITERAL like on-prem: topic names are the deployment's, so a prefix rule would have to guess.
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
