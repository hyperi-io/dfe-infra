terraform {
  required_providers {
    vault = {
      source  = "hashicorp/vault"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

variable "vault_addr" {
  description = "OpenBao/Vault server address (e.g. https://bao.example.com:8200)"
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
  description = "Target cloud (local/aws/gcp/azure)"
  type        = string
}

variable "kafka_provider" {
  description = <<-EOT
    Broker provider whose service-user credential gets seeded, and the last path
    segment of its store key (<project>/<env>/kafka/<provider>). Must match the
    kafka chart's kafka.provider, which is what reads the key back -- they are the
    two halves of one contract.
  EOT
  type        = string
  default     = "strimzi"
}
