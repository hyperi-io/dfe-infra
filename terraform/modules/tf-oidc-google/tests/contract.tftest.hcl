// Plan-only: mock_provider means no credentials, no API call and no cost.

mock_provider "google" {
  // A key's generated default is a random string, and outputs.tf base64-decodes it.
  mock_resource "google_service_account_key" {
    defaults = {
      private_key = "e30="
    }
  }
}

variables {
  project_id = "dfe-contract"
}

run "the_group_resolver_service_account_is_planned" {
  command = plan

  assert {
    condition     = google_service_account.dfe_groups.account_id == "dfe-group-resolver"
    error_message = "the Admin SDK group resolver's service account id must stay dfe-group-resolver"
  }

  assert {
    condition     = google_service_account.dfe_groups.project == "dfe-contract"
    error_message = "the service account must land in the caller's project_id"
  }

  assert {
    condition     = output.issuer == "https://accounts.google.com"
    error_message = "the issuer output must be Google's OIDC issuer"
  }
}
