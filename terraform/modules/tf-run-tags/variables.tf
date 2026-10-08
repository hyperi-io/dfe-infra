// The run identity and expiry convention, OpenTofu half. scripts/cloud_run.py
// is the other half and owns the defaults; scripts/tests/test_cloud_run.py
// holds the two equal.

variable "run" {
  description = "The test run this apply belongs to, or null for a deployment that is not one. id is the run id (lowercase letters, digits, '_' or '-', at most 63 characters, so a GCP label accepts it); expires_at is epoch seconds after which a reaper may remove what the run left behind. keys renames the two tags for an organisation whose tag policy reserves the defaults, and keys.format writes the expiry as iso8601 (UTC, the default, what an AWS account guardrail reads) or epoch-seconds (plain seconds, the only form a GCP label can hold)."
  type = object({
    id         = string
    expires_at = number
    keys = optional(object({
      run    = optional(string, "dfe-e2e")
      expiry = optional(string, "expires-at")
      format = optional(string, "iso8601")
    }), {})
  })
  default = null

  validation {
    condition     = var.run == null || can(regex("^[a-z0-9_-]{1,63}$", var.run.id))
    error_message = "run.id must be 1 to 63 lowercase letters, digits, '_' or '-' -- the value every cloud's tag or label accepts."
  }

  validation {
    condition     = var.run == null || try(var.run.expires_at > 0 && floor(var.run.expires_at) == var.run.expires_at, false)
    error_message = "run.expires_at must be a positive whole number of epoch seconds."
  }

  validation {
    condition = var.run == null || try(alltrue([
      for key in [var.run.keys.run, var.run.keys.expiry] : can(regex("^[a-z][a-z0-9_-]{0,62}$", key))
    ]) && var.run.keys.run != var.run.keys.expiry, false)
    error_message = "run.keys must be two different keys, each a lowercase letter followed by up to 62 lowercase letters, digits, '_' or '-'."
  }

  validation {
    condition     = var.run == null || try(contains(["iso8601", "epoch-seconds"], var.run.keys.format), false)
    error_message = "run.keys.format must be iso8601 or epoch-seconds."
  }
}
