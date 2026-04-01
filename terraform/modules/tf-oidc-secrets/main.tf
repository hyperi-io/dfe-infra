# Seed empty secret paths in OpenBao for each OIDC provider.
# Operators fill in actual values via bao-admin or CI.
# lifecycle.ignore_changes prevents Terraform from overwriting operator-set values.
resource "vault_kv_secret_v2" "oidc_provider" {
  for_each = var.providers

  mount = var.vault_mount
  name  = "${var.project}/${var.env}/oidc/${each.key}"

  data_json = jsonencode({
    for key in each.value.secret_keys : key => ""
  })

  lifecycle {
    ignore_changes = [data_json]
  }
}
