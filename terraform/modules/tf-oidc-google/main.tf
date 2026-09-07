terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
  }
}

# OAuth2 consent screen
resource "google_iap_brand" "dfe" {
  support_email     = "admin@${var.google_workspace_domain}"
  application_title = "DFE Platform"
  project           = var.project_id
}

# OAuth2 client (web application)
resource "google_iap_client" "dfe" {
  display_name = "DFE OIDC"
  brand        = google_iap_brand.dfe.name
}

# Service account for Admin SDK group resolution
resource "google_service_account" "dfe_groups" {
  account_id   = "dfe-group-resolver"
  display_name = "DFE Group Resolver"
  description  = "Service account for Google Admin SDK group lookups"
  project      = var.project_id
}

resource "google_service_account_key" "dfe_groups" {
  service_account_id = google_service_account.dfe_groups.name
}
