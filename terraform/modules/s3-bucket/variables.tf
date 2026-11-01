variable "name" {
  description = "The bucket's name. S3's namespace is global, so the caller qualifies it (account id, s3_bucket_prefix)."
  type        = string
}

variable "retain" {
  description = "true for a persistent deployment: tofu destroy forgets the bucket and its configuration (lifecycle.destroy = false) and the data stays. false for an ephemeral one: the bucket is force-destroyed with its objects."
  type        = bool
}

variable "kms_key_arn" {
  description = "The customer-managed key every object is encrypted under (SSE-KMS, bucket key on)."
  type        = string
}

variable "versioning" {
  description = "Keep noncurrent versions, so a delete can be recovered from. Pair it with a noncurrent_expiration_days rule."
  type        = bool
}

variable "policy_statements" {
  description = "Bucket policy statements on top of the DenyInsecureTransport this module always writes. A statement naming the bucket builds its ARN from var.name, because the bucket's own arn is not known before it exists."
  type        = any
  default     = []
}

variable "lifecycle_rules" {
  description = "Lifecycle rules over the whole bucket. Each sets any of expiration_days, noncurrent_expiration_days and abort_multipart_days, from the caller's tfvars or its own named housekeeping local."
  type = list(object({
    id                         = string
    expiration_days            = optional(number)
    noncurrent_expiration_days = optional(number)
    abort_multipart_days       = optional(number)
  }))
  default = []
}

variable "tags" {
  description = "Tags on the bucket, on top of the provider's default_tags."
  type        = map(string)
  default     = {}
}
