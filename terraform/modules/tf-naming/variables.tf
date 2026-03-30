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
  description = "Deployment environment. Must be one of: dev, stg, prod, local."
  type        = string

  validation {
    condition     = contains(["dev", "stg", "prod", "local"], var.env)
    error_message = "env must be one of: dev, stg, prod, local."
  }
}

variable "cloud" {
  description = "Target cloud platform. Must be one of: aws, gcp, az, local."
  type        = string

  validation {
    condition     = contains(["aws", "gcp", "az", "local"], var.cloud)
    error_message = "cloud must be one of: aws, gcp, az, local."
  }
}

variable "region" {
  description = "Cloud region identifier (e.g. 'us-east-1', 'europe-west1'). Use 'local' for on-prem Rancher deployments."
  type        = string
  default     = "local"
}
