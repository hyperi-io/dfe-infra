# terraform/modules/tf-naming/main.tf
#
# Derives all platform-specific resource names from four canonical dimensions.
# Canonical name: {project}-{component}-{env} — max 30 chars (GCP Service Account ID constraint).
# See spec Section 9 for the full standard.

locals {
  canonical_name = "${var.project}-${var.component}-${var.env}"

  k8s_namespace       = "${var.project}-${var.env}"
  k8s_service_account = "${var.project}-${var.component}"

  rancher_cluster_name = "${var.project}-${var.cloud}-${var.env}"
  rancher_project_name = "${var.project}-${var.env}"

  aws_irsa_role_name = "${local.canonical_name}-irsa"

  gcp_sa_account_id = local.canonical_name

  azure_identity_name       = "id-${local.canonical_name}"
  azure_federated_cred_name = "fc-${local.canonical_name}"

  vault_approle_name = local.canonical_name
  vault_policy_name  = "${local.canonical_name}-policy"
  vault_secret_path  = "secret/data/${var.project}/${var.env}/${var.component}"

  common_tags = {
    project    = var.project
    component  = var.component
    env        = var.env
    cloud      = var.cloud
    region     = var.region
    managed_by = "terraform"
    repo       = "github.com/hyperi-io/dfe-infra"
  }
}

resource "null_resource" "validate_canonical_name_length" {
  lifecycle {
    precondition {
      condition     = length(local.canonical_name) <= 30
      error_message = "Canonical name '${local.canonical_name}' is ${length(local.canonical_name)} chars, exceeds the 30-char GCP Service Account ID limit. Shorten project, component, or env."
    }
  }
}
