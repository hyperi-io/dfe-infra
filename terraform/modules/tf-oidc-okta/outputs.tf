output "client_id" {
  value = okta_app_oauth.dfe.client_id
}

output "client_secret" {
  value     = okta_app_oauth.dfe.client_secret
  sensitive = true
}

output "issuer" {
  value = "https://${var.okta_domain}/oauth2/default"
}
