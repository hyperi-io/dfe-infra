terraform {
  required_providers {
    okta = {
      source  = "okta/okta"
      version = ">= 4.0"
    }
  }
}

resource "okta_app_oauth" "dfe" {
  label                      = "DFE Platform"
  type                       = "web"
  grant_types                = ["authorization_code"]
  redirect_uris              = ["https://dfe.${var.domain}/oauth2/callback"]
  response_types             = ["code"]
  token_endpoint_auth_method = "client_secret_post"
}
