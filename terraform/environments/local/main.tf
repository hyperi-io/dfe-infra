terraform {
  # 1.9 is the first OpenTofu whose variable validation can read another variable.
  required_version = ">= 1.9"

  required_providers {
    vault = {
      source  = "hashicorp/vault"
      version = "~> 5.0"
    }
  }
}

# Only the openbao backend plans a vault object, so with none an empty address is
# never dialled. A null token leaves the provider's own VAULT_TOKEN and
# ~/.vault-token lookup.
provider "vault" {
  address = var.vault_addr
  token   = var.vault_token != "" ? var.vault_token : null
}

locals {
  project = "dfe"
  env     = "local"
  cloud   = "local"
  // On-prem has no cloud region, so the region tag carries the cloud token.
  region = "local"

  // As in dfe-ops, a named backend wins and an OpenBao address keeps openbao.
  secrets_backend = var.secrets_backend != "" ? var.secrets_backend : (var.vault_addr != "" ? "openbao" : "none")
  openbao         = local.secrets_backend == "openbao"

  // The profiles whose charts read the store on-prem: scale's in-cluster broker
  // takes its client credential from it. scripts/tests holds this to dfe-ops' _store_consumers.
  store_profiles = ["scale"]
}

module "naming" {
  source = "../../modules/tf-naming"

  project   = local.project
  component = "cluster"
  env       = local.env
  cloud     = local.cloud
  region    = local.region
}

module "secrets" {
  count  = local.openbao ? 1 : 0
  source = "../../modules/tf-secrets"

  vault_addr = var.vault_addr
  project    = local.project
  env        = local.env
  cloud      = local.cloud
}

module "iam" {
  count  = local.openbao ? 1 : 0
  source = "../../modules/tf-iam"

  project                    = local.project
  env                        = local.env
  cloud                      = local.cloud
  region                     = local.region
  vault_approle_backend_path = "approle"
  kv_mount_path              = module.secrets[0].kv_mount_path

  depends_on = [module.secrets]
}

# An OpenBao deployment applied before the store became optional keeps its objects,
# the KV mount above all, which prevent_destroy would otherwise refuse to replace.
moved {
  from = module.secrets
  to   = module.secrets[0]
}

moved {
  from = module.iam
  to   = module.iam[0]
}

module "storage" {
  source = "../../modules/tf-storage"

  cloud          = local.cloud
  nfs_server     = var.nfs_server
  minio_endpoint = var.minio_endpoint
}
