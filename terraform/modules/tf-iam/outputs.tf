output "workload_identity_annotations" {
  description = "Map of service → K8s SA annotations for workload identity. Empty for local (AppRole-based)."
  value = {
    for svc in var.services : svc => {}
  }
}

output "service_approle_names" {
  description = "Map of service → AppRole name in OpenBao"
  value = {
    for svc in var.services : svc => vault_approle_auth_backend_role.service[svc].role_name
  }
}

output "service_policy_names" {
  description = "Map of service → Vault policy name"
  value = {
    for svc in var.services : svc => vault_policy.service[svc].name
  }
}
