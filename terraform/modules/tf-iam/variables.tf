terraform {
  required_providers {
    vault = {
      source  = "hashicorp/vault"
      version = "~> 5.0"
    }
  }
}

variable "project" {
  description = "Project identifier (from tf-naming)"
  type        = string
  default     = "dfe"
}

variable "env" {
  description = "Deployment environment"
  type        = string
}

variable "cloud" {
  description = "Target cloud platform"
  type        = string
}

variable "services" {
  description = "List of DFE service names that need workload identity"
  type        = list(string)
  default     = ["engine", "ui", "receiver", "loader", "archiver", "fetcher"]
}

variable "vault_approle_backend_path" {
  description = "AppRole auth backend path (created by tf-secrets)"
  type        = string
  default     = "approle"
}

variable "kv_mount_path" {
  description = "KV v2 mount path (created by tf-secrets)"
  type        = string
  default     = "secret"
}
