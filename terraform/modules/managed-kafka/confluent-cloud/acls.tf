// The DFE principal, a manager principal that writes its ACLs, and the grants. dfe-engine creates the topics.
// The grant set, why two accounts, and where tofu has to run: docs/deployment/kafka/confluent-cloud.md#module-design-notes.

locals {
  topic_grants = toset(["READ", "WRITE", "CREATE", "DESCRIBE", "DELETE", "ALTER", "DESCRIBE_CONFIGS", "ALTER_CONFIGS"])

  group_grants = toset(["READ", "DESCRIBE"])

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

resource "confluent_service_account" "manager" {
  display_name = "${var.name}-manager"
  description  = "Writes the ACLs for ${var.name} (${var.env}). Never used by DFE."
}

resource "confluent_role_binding" "manager" {
  principal = "User:${confluent_service_account.manager.id}"

  role_name   = "CloudClusterAdmin"
  crn_pattern = confluent_kafka_cluster.this.rbac_crn
}

resource "confluent_api_key" "manager" {
  // The role binding lands before the key writes anything, which orders every ACL behind it.
  depends_on = [confluent_role_binding.manager]

  display_name = "${var.name}-manager-kafka-api-key"
  description  = "Kafka API key for the ${var.name} manager service account"

  // The provider's wait fails the apply on the first reset, so terraform_data.rest_ready waits instead.
  disable_wait_for_ready = true

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
      id = local.environment_id
    }
  }
}

resource "confluent_service_account" "dfe" {
  display_name = "${var.name}-${var.service_account_name}"
  description  = "The one principal the DFE fleet authenticates as on ${var.name} (${var.env})"
}

resource "confluent_api_key" "dfe" {
  display_name = "${var.name}-${var.service_account_name}-kafka-api-key"
  description  = "SASL PLAIN credential for the DFE fleet on ${var.name}"

  // Tofu never signs in with this key, so the provider's wait for a 200 would only fail the apply.
  disable_wait_for_ready = true

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
      id = local.environment_id
    }
  }
}

resource "confluent_kafka_acl" "dfe" {
  for_each = local.acls

  // Without the wait, a reset or a 401 fails the apply.
  depends_on = [terraform_data.rest_ready]

  kafka_cluster {
    id = confluent_kafka_cluster.this.id
  }

  resource_type = each.value.resource_type
  resource_name = each.value.resource_name
  pattern_type  = each.value.pattern_type

  // Confluent's principal is the service account's id, not its name.
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
