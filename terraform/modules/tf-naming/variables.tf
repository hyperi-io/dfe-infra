terraform {
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
  }
}

variable "project" {
  description = "Project identifier. Must be 2-10 lowercase alphanumeric chars starting with a letter. Default: 'dfe'."
  type        = string
  default     = "dfe"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,9}$", var.project))
    error_message = "project must be 2-10 lowercase alphanumeric chars starting with a letter (e.g. 'dfe')."
  }
}

variable "component" {
  description = "Component name. 2-24 chars, lowercase letters/digits/hyphens, start and end with letter or digit. Total canonical name ({project}-{component}-{env}) must be <=30 chars."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,22}[a-z0-9]$", var.component)) || can(regex("^[a-z]{2}$", var.component))
    error_message = "component must be 2-24 chars: lowercase letters, digits, hyphens — start and end with letter or digit."
  }
}

variable "env" {
  description = "Deployment environment: dev, test, staging, prod, local, or customer-<id>."
  type        = string

  // One vocabulary for the environment, everywhere. dev / test / staging /
  // prod / customer-<id> is what the deployment dial, the governance tags and
  // the cloud roots all say; local is the on-prem Rancher path's own. "stg"
  // was a fifth spelling of staging that nothing else in the tree used.
  validation {
    condition     = contains(["dev", "test", "staging", "prod", "local"], var.env) || can(regex("^customer-[a-z0-9][a-z0-9-]*$", var.env))
    error_message = "env must be dev, test, staging, prod, local, or customer-<id>."
  }
}

variable "cloud" {
  description = "Target cloud platform. Must be one of: aws, gcp, azure, local."
  type        = string

  // One token names the deployment root, the argocd/values/<cloud>.yaml overlay,
  // the shapes key and the state path, so it has to be the same word in all of
  // them. `az` was the CLI's name for it and nothing else in the tree said it.
  validation {
    condition     = contains(["aws", "gcp", "azure", "local"], var.cloud)
    error_message = "cloud must be one of: aws, gcp, azure, local."
  }
}

variable "region" {
  description = "Cloud region identifier (e.g. 'us-east-1', 'europe-west1'). Use 'local' for on-prem Rancher deployments."
  type        = string
  default     = "local"
}
