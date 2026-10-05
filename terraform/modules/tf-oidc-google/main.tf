terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
  }
}

# The OAuth client for Google sign-in is created in the Cloud console, because the IAP OAuth Admin API that could create it is shut down.

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
