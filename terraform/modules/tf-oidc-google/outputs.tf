output "client_id" {
  value = google_iap_client.dfe.client_id
}

output "client_secret" {
  value     = google_iap_client.dfe.secret
  sensitive = true
}

output "service_account_email" {
  value = google_service_account.dfe_groups.email
}

output "service_account_key_json" {
  value     = base64decode(google_service_account_key.dfe_groups.private_key)
  sensitive = true
}

output "issuer" {
  value = "https://accounts.google.com"
}
