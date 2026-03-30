# Integration test — requires live Vault/OpenBao.
# Run with: VAULT_ADDR=... VAULT_TOKEN=... terraform test
variables {
  project   = "dfe"
  env       = "test"
  cloud     = "local"
  services  = ["loader", "receiver"]
}

run "per_service_outputs" {
  command = plan

  assert {
    condition     = length(output.service_approle_names) == 2
    error_message = "should create AppRole per service (expected 2)"
  }

  assert {
    condition     = output.service_approle_names["loader"] == "dfe-loader-test"
    error_message = "loader AppRole name should be dfe-loader-test"
  }

  assert {
    condition     = length(output.workload_identity_annotations) == 2
    error_message = "should output workload_identity_annotations per service"
  }
}
