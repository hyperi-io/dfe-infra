# Configures the secrets backend for ESO integration.
# For local/Rancher: OpenBao with AppRole auth.

resource "vault_mount" "dfe_kv" {
  path        = "secret"
  type        = "kv-v2"
  description = "DFE KV secrets engine"

  lifecycle {
    prevent_destroy = true
  }
}

resource "vault_auth_backend" "approle" {
  type = "approle"
  path = "approle"
}

resource "vault_policy" "eso" {
  name   = "${var.project}-eso-${var.env}"
  policy = <<-EOT
    path "secret/data/${var.project}/${var.env}/*" {
      capabilities = ["read", "list"]
    }
    path "secret/metadata/${var.project}/${var.env}/*" {
      capabilities = ["read", "list"]
    }
  EOT
}

resource "vault_approle_auth_backend_role" "eso" {
  backend   = vault_auth_backend.approle.path
  role_name = "${var.project}-eso-${var.env}"

  token_policies = [vault_policy.eso.name]
  token_ttl      = 3600
  token_max_ttl  = 86400
  token_num_uses = 0
}

resource "vault_approle_auth_backend_role_secret_id" "eso" {
  backend   = vault_auth_backend.approle.path
  role_name = vault_approle_auth_backend_role.eso.role_name
}

resource "vault_kv_secret_v2" "seed_argocd" {
  mount = vault_mount.dfe_kv.path
  name  = "${var.project}/${var.env}/argocd"
  data_json = jsonencode({
    admin_password = ""
  })

  lifecycle {
    ignore_changes = [data_json]
  }
}

# The Kafka service user's SCRAM password.
#
# Seeded HERE, by the deploy layer, rather than generated in-cluster: the store is
# the source of truth and the ESO policy above is deliberately read-only, so ESO
# projects this credential but never writes it. (An in-cluster ESO PushSecret was
# tried and is refused by that policy -- 403 on PUT secret/metadata/... -- which is
# the policy working as designed, not a bug to route around.)
#
# The store is also the ONE seam that swaps for a public-cloud deploy: on EKS this
# resource becomes aws_secretsmanager_secret_version and the ClusterSecretStore's
# provider flips to aws, while every reader downstream is unchanged.
#
# alphanumeric (special = false): the password rides a JAAS config string and a
# broker properties file, where punctuation needs escaping. Same reasoning as the
# ESO Password generator's `symbols: 0` in kafka-single-user.yaml.
resource "random_password" "kafka_user" {
  length  = 32
  special = false
}

resource "vault_kv_secret_v2" "seed_kafka_user" {
  mount = vault_mount.dfe_kv.path
  # Read back by the kafka chart's dfe-kafka.credentialKey helper -- one contract,
  # two halves. RELATIVE to the `secret` mount above, which the store pins itself.
  name = "${var.project}/${var.env}/kafka/${var.kafka_provider}"
  data_json = jsonencode({
    password = random_password.kafka_user.result
  })

  # NEVER rotate on re-apply. A Strimzi KafkaUser keeps the password it was given,
  # so a regenerated value would leave the broker authenticating against a password
  # the store no longer holds -- locking every DFE service out of Kafka. Same reason
  # seed_argocd ignores changes. Rotation is a deliberate, separate operation.
  lifecycle {
    ignore_changes = [data_json]
  }
}
