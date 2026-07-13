# Plan-only test of the naming + per-service output wiring. tf-iam only CREATES
# vault resources (no data reads), so `plan` needs the provider CONFIGURED but
# never CONNECTS -- a dummy address + skip_child_token keeps it offline (CI has
# no Vault). A full apply against live Vault/OpenBao is a separate integration
# concern (VAULT_ADDR/VAULT_TOKEN).
variables {
  project   = "dfe"
  env       = "local"
  cloud     = "local"
  services  = ["loader", "receiver"]
}

provider "vault" {
  address          = "http://127.0.0.1:8200"
  token            = "dummy-token-for-plan"
  skip_child_token = true
}

run "per_service_outputs" {
  command = plan

  assert {
    condition     = length(output.service_approle_names) == 2
    error_message = "should create AppRole per service (expected 2)"
  }

  assert {
    condition     = output.service_approle_names["loader"] == "dfe-loader-local"
    error_message = "loader AppRole name should be dfe-loader-local"
  }

  assert {
    condition     = length(output.workload_identity_annotations) == 2
    error_message = "should output workload_identity_annotations per service"
  }
}
