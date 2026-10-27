// The on-prem root with OpenBao: an address selects it on any profile, scale
// plans with it, and the KV mount, the ESO AppRole and the per-service AppRoles
// are all planned. Every provider is mocked, so no OpenBao is needed.

mock_provider "vault" {}

mock_provider "random" {}

variables {
  domain     = "dfe.example.com"
  profile    = "slim"
  nfs_server = "storage.example.com"
  repo_url   = "https://git.example.invalid/dfe-infra.git"
  vault_addr = "https://bao.example.com:8200"
}

run "an_address_selects_openbao" {
  command = plan

  assert {
    condition     = output.DFE_SECRETS_BACKEND == "openbao"
    error_message = "an OpenBao address with no named backend keeps openbao"
  }

  assert {
    condition     = length(module.secrets) == 1 && length(module.iam) == 1
    error_message = "openbao plans the store's objects and the per-service AppRoles"
  }

  assert {
    condition     = output.DFE_VAULT_ADDR == "https://bao.example.com:8200"
    error_message = "bootstrap gets the address ESO dials"
  }

  assert {
    condition     = output.DFE_WORKLOAD_IDENTITY_ANNOTATIONS == jsonencode(module.iam[0].workload_identity_annotations)
    error_message = "openbao keeps tf-iam's per-service annotations"
  }
}

run "scale_plans_with_openbao" {
  command = plan

  variables {
    profile = "scale"
  }

  assert {
    condition     = output.DFE_SECRETS_BACKEND == "openbao"
    error_message = "scale with an address resolves to openbao"
  }
}

run "a_named_none_wins_over_an_address" {
  command = plan

  variables {
    secrets_backend = "none"
  }

  assert {
    condition     = output.DFE_SECRETS_BACKEND == "none" && length(module.secrets) == 0
    error_message = "a named backend wins, as it does in dfe-ops"
  }
}
