terraform {
  required_version = ">= 1.6.0"

  required_providers {
    vault = {
      source  = "hashicorp/vault"
      version = "~> 4.0"
    }
  }
}

provider "vault" {
  address = var.vault_addr
  token   = var.vault_token
}

locals {
  project = "dfe"
  env     = "local"
  cloud   = "local"
  region  = "local"
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
  source = "../../modules/tf-secrets"

  vault_addr = var.vault_addr
  project    = local.project
  env        = local.env
  cloud      = local.cloud
}

module "iam" {
  source = "../../modules/tf-iam"

  project                    = local.project
  env                        = local.env
  cloud                      = local.cloud
  vault_approle_backend_path = "approle"
  kv_mount_path              = module.secrets.kv_mount_path

  depends_on = [module.secrets]
}

module "storage" {
  source = "../../modules/tf-storage"

  cloud          = local.cloud
  nfs_server     = var.nfs_server
  minio_endpoint = var.minio_endpoint
}
