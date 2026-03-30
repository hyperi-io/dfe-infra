# Creates per-service AppRoles in OpenBao (local target).
# Cloud targets (aws/gcp/az) replace this with IRSA/WIF/Azure MI.

module "naming" {
  source = "../tf-naming"

  for_each  = toset(var.services)
  project   = var.project
  component = each.key
  env       = var.env
  cloud     = var.cloud
}

resource "vault_policy" "service" {
  for_each = toset(var.services)

  name   = module.naming[each.key].vault_policy_name
  policy = <<-EOT
    path "${var.kv_mount_path}/data/${var.project}/${var.env}/${each.key}/*" {
      capabilities = ["read", "list"]
    }
    path "${var.kv_mount_path}/metadata/${var.project}/${var.env}/${each.key}/*" {
      capabilities = ["read", "list"]
    }
  EOT
}

resource "vault_approle_auth_backend_role" "service" {
  for_each = toset(var.services)

  backend   = var.vault_approle_backend_path
  role_name = module.naming[each.key].vault_approle_name

  token_policies  = [vault_policy.service[each.key].name]
  token_ttl       = 3600
  token_max_ttl   = 86400
}
