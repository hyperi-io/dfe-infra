output "vault_addr" {
  description = "OpenBao/Vault server address"
  value       = var.vault_addr
}

output "eso_role_id" {
  description = "AppRole role_id for ESO to authenticate with OpenBao"
  value       = vault_approle_auth_backend_role.eso.role_id
}

output "eso_secret_id" {
  description = "AppRole secret_id for ESO (sensitive)"
  value       = vault_approle_auth_backend_role_secret_id.eso.secret_id
  sensitive   = true
}

output "eso_policy_name" {
  description = "Vault policy name assigned to ESO AppRole"
  value       = vault_policy.eso.name
}

output "kv_mount_path" {
  description = "KV v2 secrets engine mount path"
  value       = vault_mount.dfe_kv.path
}
