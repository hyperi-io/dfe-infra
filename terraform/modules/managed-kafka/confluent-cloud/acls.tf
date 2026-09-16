// The DFE principal, its grants and the landing topics.
//
// TWO service accounts, for the same reason MSK enables both SASL/IAM and
// SASL/SCRAM: something has to be allowed to write the first ACL, and that
// something must not be the credential DFE runs as. The manager account holds
// CloudClusterAdmin and exists only for tofu; the DFE account holds the grant
// set kafka-user.yaml already gives on-prem -- topic * with Read, Write, Create
// and Describe, and consumer groups fenced to the dfe- prefix with Read and
// Describe. No Delete, no Alter, no cluster operations.
//
// The credential is an API key and secret used as SASL PLAIN. Confluent has no
// SCRAM on any tier.
//
// Topics and ACLs are data-plane resources reached over the REST endpoint, so
// on private connectivity tofu has to run somewhere that can resolve and reach
// it -- inside the VPC, or behind a resolver rule that forwards into it.

locals {
  topic_grants = toset(["READ", "WRITE", "CREATE", "DESCRIBE"])

  group_grants = toset(["READ", "DESCRIBE"])

  // Every ACL and topic this module writes, at one grant apiece.
  acls = merge(
    {
      for operation in local.topic_grants : "topic-${lower(operation)}" => {
        resource_type = "TOPIC"
        resource_name = "*"
        pattern_type  = "LITERAL"
        operation     = operation
      }
    },
    {
      for operation in local.group_grants : "group-${lower(operation)}" => {
        resource_type = "GROUP"
        resource_name = var.consumer_group_prefix
        pattern_type  = "PREFIXED"
        operation     = operation
      }
    }
  )
}

// ---------------------------------------------------------------------------
// The identity tofu writes with
// ---------------------------------------------------------------------------

resource "confluent_service_account" "manager" {
  display_name = "${var.name}-manager"
  description  = "Writes the topics and ACLs for ${var.name} (${var.env}). Never used by DFE."
}

resource "confluent_role_binding" "manager" {
  principal = "User:${confluent_service_account.manager.id}"

  // Confluent's own role name for a cluster administrator.
  role_name   = "CloudClusterAdmin"
  crn_pattern = confluent_kafka_cluster.this.rbac_crn
}

resource "confluent_api_key" "manager" {
  display_name = "${var.name}-manager-kafka-api-key"
  description  = "Kafka API key for the ${var.name} manager service account"

  // A private cluster answers no readiness probe from outside its network, so
  // the provider's wait would time out on a cluster that is in fact fine.
  disable_wait_for_ready = local.private

  owner {
    id          = confluent_service_account.manager.id
    api_version = confluent_service_account.manager.api_version
    kind        = confluent_service_account.manager.kind
  }

  managed_resource {
    id          = confluent_kafka_cluster.this.id
    api_version = confluent_kafka_cluster.this.api_version
    kind        = confluent_kafka_cluster.this.kind

    environment {
      id = confluent_environment.this.id
    }
  }

  // The role binding has to land before this key is used to write anything,
  // and naming it here keeps the dependency in one place rather than on every
  // topic and ACL below.
  depends_on = [confluent_role_binding.manager]
}

// ---------------------------------------------------------------------------
// The identity DFE runs as
// ---------------------------------------------------------------------------

resource "confluent_service_account" "dfe" {
  display_name = "${var.name}-${var.service_account_name}"
  description  = "The one principal the DFE fleet authenticates as on ${var.name} (${var.env})"
}

resource "confluent_api_key" "dfe" {
  display_name = "${var.name}-${var.service_account_name}-kafka-api-key"
  description  = "SASL PLAIN credential for the DFE fleet on ${var.name}"

  disable_wait_for_ready = local.private

  owner {
    id          = confluent_service_account.dfe.id
    api_version = confluent_service_account.dfe.api_version
    kind        = confluent_service_account.dfe.kind
  }

  managed_resource {
    id          = confluent_kafka_cluster.this.id
    api_version = confluent_kafka_cluster.this.api_version
    kind        = confluent_kafka_cluster.this.kind

    environment {
      id = confluent_environment.this.id
    }
  }
}

// ---------------------------------------------------------------------------
// The grants and the topics
// ---------------------------------------------------------------------------

resource "confluent_kafka_acl" "dfe" {
  for_each = local.acls

  kafka_cluster {
    id = confluent_kafka_cluster.this.id
  }

  resource_type = each.value.resource_type
  resource_name = each.value.resource_name
  pattern_type  = each.value.pattern_type

  // Confluent's principal is the service account's own id, not its name.
  principal  = "User:${confluent_service_account.dfe.id}"
  host       = "*"
  operation  = each.value.operation
  permission = "ALLOW"

  rest_endpoint = confluent_kafka_cluster.this.rest_endpoint

  credentials {
    key    = confluent_api_key.manager.id
    secret = confluent_api_key.manager.secret
  }
}

// Pre-created, never conjured by the first produce: dfe-loader treats a missing
// landing topic as fatal, and pre-creating is what makes the partition count
// and the retention ours to set.
resource "confluent_kafka_topic" "landing" {
  for_each = var.landing_topics

  kafka_cluster {
    id = confluent_kafka_cluster.this.id
  }

  topic_name       = each.key
  partitions_count = coalesce(each.value.partitions, var.num_partitions)

  config = {
    "cleanup.policy"    = "delete"
    "retention.ms"      = tostring(coalesce(each.value.retention_ms, var.log_retention_ms))
    "max.message.bytes" = tostring(var.message_max_bytes)
  }

  rest_endpoint = confluent_kafka_cluster.this.rest_endpoint

  credentials {
    key    = confluent_api_key.manager.id
    secret = confluent_api_key.manager.secret
  }
}
