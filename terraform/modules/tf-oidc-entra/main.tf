terraform {
  required_providers {
    azuread = {
      source  = "hashicorp/azuread"
      version = ">= 3.0"
    }
  }
}

resource "azuread_application" "dfe" {
  display_name = var.display_name

  web {
    redirect_uris = ["https://dfe.${var.domain}/oauth2/callback"]
  }

  required_resource_access {
    resource_app_id = "00000003-0000-0000-c000-000000000000" # Microsoft Graph

    resource_access {
      id   = "98830695-27a2-44f7-8c18-0c3ebc9698f6" # GroupMember.Read.All
      type = "Role"                                 # Application permission (admin consented)
    }
  }
}

resource "azuread_application_password" "dfe" {
  application_id = azuread_application.dfe.id
  display_name   = "DFE OIDC client secret"
}
