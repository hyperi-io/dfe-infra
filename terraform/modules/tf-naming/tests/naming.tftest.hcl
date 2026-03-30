# terraform/modules/tf-naming/tests/naming.tftest.hcl

# --- Happy path: standard dfe-loader-prod on AWS ---
variables {
  project   = "dfe"
  component = "loader"
  env       = "prod"
  cloud     = "aws"
  region    = "us-east-1"
}

run "canonical_name_format" {
  command = plan

  assert {
    condition     = output.canonical_name == "dfe-loader-prod"
    error_message = "canonical_name must be {project}-{component}-{env}"
  }
}

run "k8s_names" {
  command = plan

  assert {
    condition     = output.k8s_namespace == "dfe-prod"
    error_message = "k8s_namespace must be {project}-{env}"
  }

  assert {
    condition     = output.k8s_service_account == "dfe-loader"
    error_message = "k8s_service_account must be {project}-{component}"
  }
}

run "rancher_names" {
  command = plan

  assert {
    condition     = output.rancher_cluster_name == "dfe-aws-prod"
    error_message = "rancher_cluster_name must be {project}-{cloud}-{env}"
  }

  assert {
    condition     = output.rancher_project_name == "dfe-prod"
    error_message = "rancher_project_name must be {project}-{env}"
  }
}

run "aws_names" {
  command = plan

  assert {
    condition     = output.aws_irsa_role_name == "dfe-loader-prod-irsa"
    error_message = "aws_irsa_role_name must be {canonical_name}-irsa"
  }
}

run "gcp_names" {
  command = plan

  assert {
    condition     = output.gcp_sa_account_id == "dfe-loader-prod"
    error_message = "gcp_sa_account_id must equal canonical_name (≤30 chars for GCP)"
  }
}

run "azure_names" {
  command = plan

  assert {
    condition     = output.azure_identity_name == "id-dfe-loader-prod"
    error_message = "azure_identity_name must be id-{canonical_name}"
  }

  assert {
    condition     = output.azure_federated_cred_name == "fc-dfe-loader-prod"
    error_message = "azure_federated_cred_name must be fc-{canonical_name}"
  }
}

run "vault_names" {
  command = plan

  assert {
    condition     = output.vault_approle_name == "dfe-loader-prod"
    error_message = "vault_approle_name must equal canonical_name"
  }

  assert {
    condition     = output.vault_policy_name == "dfe-loader-prod-policy"
    error_message = "vault_policy_name must be {canonical_name}-policy"
  }

  assert {
    condition     = output.vault_secret_path == "secret/data/dfe/prod/loader"
    error_message = "vault_secret_path must be secret/data/{project}/{env}/{component}"
  }
}

run "common_tags_mandatory_keys" {
  command = plan

  assert {
    condition     = output.common_tags["project"] == "dfe"
    error_message = "common_tags.project must equal var.project"
  }

  assert {
    condition     = output.common_tags["component"] == "loader"
    error_message = "common_tags.component must equal var.component"
  }

  assert {
    condition     = output.common_tags["env"] == "prod"
    error_message = "common_tags.env must equal var.env"
  }

  assert {
    condition     = output.common_tags["managed_by"] == "terraform"
    error_message = "common_tags.managed_by must be 'terraform'"
  }

  assert {
    condition     = output.common_tags["repo"] == "github.com/hyperi-io/dfe-infra"
    error_message = "common_tags.repo must be canonical dfe-infra repo path"
  }
}

run "canonical_name_within_30_chars" {
  command = plan

  assert {
    condition     = length(output.canonical_name) <= 30
    error_message = "canonical_name must be <=30 chars (GCP Service Account ID constraint)"
  }
}

# --- Failure case: component name would push canonical >30 chars ---
run "reject_canonical_name_over_30_chars" {
  command = plan

  variables {
    project   = "dfe"
    component = "very-long-component-nm"
    env       = "prod"
    cloud     = "aws"
    region    = "local"
  }

  expect_failures = [
    null_resource.validate_canonical_name_length
  ]
}

# --- Local/Rancher variant ---
run "local_cloud_names" {
  command = plan

  variables {
    project   = "dfe"
    component = "receiver"
    env       = "local"
    cloud     = "local"
    region    = "local"
  }

  assert {
    condition     = output.canonical_name == "dfe-receiver-local"
    error_message = "local env canonical name should be dfe-receiver-local"
  }

  assert {
    condition     = output.rancher_cluster_name == "dfe-local-local"
    error_message = "rancher_cluster_name must be {project}-{cloud}-{env}"
  }
}
