output "secret_paths" {
  description = "OpenBao secret paths per provider"
  value = {
    for name, _ in var.providers :
    name => vault_kv_secret_v2.oidc_provider[name].name
  }
}

output "k8s_secret_names" {
  description = "K8s Secret names that ESO will create"
  value = {
    for name, _ in var.providers :
    name => "dfe-oidc-${name}"
  }
}
