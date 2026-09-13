variable "account" {
  description = "The AWS account the bucket must land in. Asserted against the caller's identity, because a state bucket in the wrong account is found months later."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account))
    error_message = "account must be a 12-digit AWS account id."
  }
}

variable "region" {
  description = "Region the bucket is created in. The state backend has to name the same one."
  type        = string
}

variable "name" {
  description = "Workload name the bucket is named after, e.g. dfe-test."
  type        = string
}

variable "env" {
  description = "Deployment environment, for the tags."
  type        = string
}

variable "bucket_prefix" {
  description = <<-EOT
    Organisation prefix. S3 bucket names are globally unique, so a name without
    one is a name someone else can already have taken -- permanently. No
    default: whoever runs this names their own organisation.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$", var.bucket_prefix))
    error_message = "bucket_prefix must be lowercase alphanumeric with hyphens, and start and end with a letter or digit."
  }
}

variable "bucket_name" {
  description = "Full bucket name, when the computed one will not do. Empty means compute it from bucket_prefix and name."
  type        = string
  default     = ""
}

variable "kms_key_arn" {
  description = "Customer-managed key for the bucket's default encryption. Empty means SSE-S3, which is free and needs no key policy -- the right answer for a bucket created before anything else exists."
  type        = string
  default     = ""
}

variable "tags" {
  description = "The governance tag set, applied through the provider's default_tags."
  type        = map(string)

  validation {
    condition = alltrue([
      for k in ["service-name", "service-namespace", "environment", "owner", "cost-center", "lifecycle", "iac-source"] :
      lookup(var.tags, k, "") != ""
    ])
    error_message = "tags must carry service-name, service-namespace, environment, owner, cost-center, lifecycle and iac-source, each non-empty."
  }
}
