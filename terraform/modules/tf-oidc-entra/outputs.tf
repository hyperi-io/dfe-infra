output "client_id" {
  value = azuread_application.dfe.client_id
}

output "client_secret" {
  value     = azuread_application_password.dfe.value
  sensitive = true
}

output "tenant_id" {
  value = var.tenant_id
}

output "issuer" {
  value = "https://login.microsoftonline.com/${var.tenant_id}/v2.0"
}
