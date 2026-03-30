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

  token_policies  = [vault_policy.eso.name]
  token_ttl       = 3600
  token_max_ttl   = 86400
  token_num_uses  = 0
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
