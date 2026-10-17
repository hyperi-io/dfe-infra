// The contract, as OpenTofu sees it. CONTRACT.md describes every input.

variable "provision" {
  description = "The AWS account id and region the cache lives in, the deployment's own."
  type = object({
    account = string
    region  = string
  })

  validation {
    condition     = can(regex("^[0-9]{12}$", var.provision.account))
    error_message = "provision.account must be a 12-digit AWS account id."
  }

  // The region reaches the destroy-time aws CLI call, so it is shaped here rather than trusted.
  validation {
    condition     = can(regex("^[a-z]{2}(-gov)?-[a-z]+-[0-9]$", var.provision.region))
    error_message = "provision.region must be an AWS region code such as us-west-2."
  }
}

variable "name" {
  description = "Name prefix for the IAM role this module creates."
  type        = string
}

variable "namespace" {
  description = "Repository path every rule prefix starts with, from tfvars: the dial's registry_cache.namespace, or the deployment's name when that is empty, so two deployments in one account and region never share a rule or a cached repository."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]+(?:[._-][a-z0-9]+)*$", var.namespace))
    error_message = "namespace must be lowercase letters and digits, joined by single '.', '_' or '-', as an ECR repository path segment."
  }
}

variable "upstreams" {
  description = "The upstream registries to cache, from tfvars (render_dial.py renders the ones the stack pulls from), keyed by the rule's own prefix under namespace. upstream_url is the registry URL ECR takes for that upstream, host the registry name an image reference carries, and requires_credential whether ECR refuses the upstream without a Secrets Manager credential, which credential_arn names. An upstream that requires one and names none gets no rule and no mirror, so nodes pull it directly."
  type = map(object({
    upstream_url        = string
    host                = string
    requires_credential = bool
    credential_arn      = optional(string, "")
  }))

  // ECR caps a rule prefix at 30 characters, slash included.
  validation {
    condition     = alltrue([for prefix in keys(var.upstreams) : length("${var.namespace}/${prefix}") <= 30])
    error_message = "Every <namespace>/<upstream key> must be 30 characters or fewer, ECR's limit on a pull-through rule prefix. Set a shorter registry_cache.namespace."
  }

  validation {
    condition     = alltrue([for prefix in keys(var.upstreams) : can(regex("^[a-z0-9]+(?:[._-][a-z0-9]+)*$", prefix))])
    error_message = "Every upstream key must be one lowercase ECR repository path segment."
  }

  // ECR reads the credential through its own service-linked role, which reaches this prefix only.
  validation {
    condition = alltrue([
      for upstream in values(var.upstreams) :
      upstream.credential_arn == "" || can(regex("^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:ecr-pullthroughcache/.+$", upstream.credential_arn))
    ])
    error_message = "A credential_arn must name a Secrets Manager secret under ecr-pullthroughcache/, the only path ECR's pull-through role can read."
  }

  validation {
    condition     = length(distinct([for upstream in values(var.upstreams) : upstream.host])) == length(var.upstreams)
    error_message = "Each host may be cached by one upstream only: containerd reads one mirror file per registry host."
  }
}

variable "expiry_days" {
  description = "Days a cached image is kept after ECR cached it, after which the next pull refetches it from the upstream. From tfvars (render_dial.py's default unless the dial's registry_cache.expiry_days names another)."
  type        = number

  validation {
    condition     = var.expiry_days == floor(var.expiry_days) && var.expiry_days >= 1
    error_message = "expiry_days must be a whole number of days, at least 1."
  }
}

variable "kms_key_arn" {
  description = "The deployment's own key, from the cluster module. Every cached repository is encrypted with it."
  type        = string
}

variable "node_role_arns" {
  description = "The node instance roles that pull through the cache, keyed by a name known at plan: the managed node groups' role and Karpenter's node role."
  type        = map(string)
}

variable "tags" {
  description = "Tags every cached repository is created with. ECR creates the repositories, so the provider's default_tags never reach them."
  type        = map(string)
}

variable "permissions_boundary" {
  description = "IAM policy ARN set as the permissions boundary on the role this module creates, or null for none."
  type        = string
  default     = null
}

variable "iam_path" {
  description = "IAM path for the role this module creates, or null for the default \"/\"."
  type        = string
  default     = null
}
