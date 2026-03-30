terraform {
  required_providers {
    vault = {
      source  = "hashicorp/vault"
      version = "~> 4.0"
    }
  }
}

variable "vault_addr" {
  description = "OpenBao/Vault server address (e.g. https://bao.devex.hyperi.io:8200)"
  type        = string
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
  description = "Target cloud (local/aws/gcp/az)"
  type        = string
}
