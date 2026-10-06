// Plan-only: mock_provider means no credentials, no API call and no cost.

mock_provider "okta" {}

variables {
  okta_domain = "dfe-contract.okta.com"
  domain      = "example.com"
}

run "the_web_client_is_planned" {
  command = plan

  assert {
    condition     = okta_app_oauth.dfe.type == "web"
    error_message = "the DFE client must be an Okta web app"
  }

  assert {
    condition     = okta_app_oauth.dfe.grant_types == toset(["authorization_code"])
    error_message = "the DFE client must use the authorization code grant only"
  }

  assert {
    condition     = okta_app_oauth.dfe.redirect_uris == tolist(["https://dfe.example.com/oauth2/callback"])
    error_message = "the redirect URI must be the DFE host's oauth2 callback"
  }

  assert {
    condition     = output.issuer == "https://dfe-contract.okta.com/oauth2/default"
    error_message = "the issuer output must be the org's default authorization server"
  }
}
