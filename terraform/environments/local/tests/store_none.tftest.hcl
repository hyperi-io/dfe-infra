// The on-prem root with no secrets store: slim applies and single plans with no
// OpenBao address and no token, and the profile whose charts read the store is
// refused before anything is planned.
//
// No mock and no provider block for vault, on purpose. The apply runs the real
// provider against no OpenBao, so any vault object in the run fails it. The only
// object it creates is tf-naming's local null_resource, which the test destroys.

variables {
  domain     = "dfe.example.com"
  profile    = "slim"
  nfs_server = "storage.example.com"
  repo_url   = "https://git.example.invalid/dfe-infra.git"
}

run "slim_runs_with_no_store" {
  command = apply

  assert {
    condition     = output.DFE_SECRETS_BACKEND == "none"
    error_message = "no vault_addr and no named backend resolves to none"
  }

  assert {
    condition     = length(module.secrets) == 0 && length(module.iam) == 0
    error_message = "none plans no OpenBao objects"
  }

  assert {
    condition     = output.DFE_VAULT_ADDR == "" && output.DFE_VAULT_ROLE_ID == "" && output.DFE_VAULT_SECRET_ID == ""
    error_message = "none hands bootstrap no OpenBao inputs"
  }

  assert {
    condition     = output.DFE_WORKLOAD_IDENTITY_ANNOTATIONS == "{}"
    error_message = "bootstrap requires the annotations map, empty on-prem"
  }
}

run "single_runs_with_no_store" {
  command = plan

  variables {
    profile = "single"
  }

  assert {
    condition     = output.DFE_SECRETS_BACKEND == "none"
    error_message = "single reads no store, so it resolves to none"
  }
}

run "scale_without_openbao_is_refused" {
  command = plan

  variables {
    profile = "scale"
  }

  expect_failures = [var.profile]
}

run "an_explicit_none_is_refused_for_scale" {
  command = plan

  variables {
    profile         = "scale"
    secrets_backend = "none"
  }

  expect_failures = [var.profile]
}

run "openbao_without_an_address_is_refused" {
  command = plan

  variables {
    secrets_backend = "openbao"
  }

  expect_failures = [var.secrets_backend]
}

run "aws_sm_is_refused_on_prem" {
  command = plan

  variables {
    secrets_backend = "aws-sm"
  }

  expect_failures = [var.secrets_backend]
}
