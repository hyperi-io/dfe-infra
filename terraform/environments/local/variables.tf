variable "secrets_backend" {
  description = "Secrets store the deployment reads, as DFE_SECRETS_BACKEND names it: openbao, none, or empty for openbao when vault_addr is set and none otherwise. bootstrap/deploy.sh passes DFE_SECRETS_BACKEND here"
  type        = string
  default     = ""

  validation {
    condition     = contains(["", "openbao", "none"], var.secrets_backend)
    error_message = "secrets_backend must be openbao, none or empty. aws-sm belongs to the aws root: a deployment keeps its managed backends on its own cloud."
  }

  validation {
    condition     = var.secrets_backend != "openbao" || var.vault_addr != ""
    error_message = "secrets_backend = openbao needs vault_addr, the OpenBao address bootstrap hands ESO."
  }
}

variable "vault_addr" {
  description = "OpenBao server address. Empty runs the deployment with no secrets store, which only a profile whose charts read none can do"
  type        = string
  default     = ""
}

variable "vault_token" {
  description = "OpenBao token for provisioning (sensitive). Empty leaves the provider's own VAULT_TOKEN and ~/.vault-token lookup"
  type        = string
  default     = ""
  sensitive   = true
}

variable "domain" {
  description = "Base domain for services (e.g. dfe.example.com)"
  type        = string
}

variable "profile" {
  description = "Deployment profile, from tfvars: slim (single-node, gRPC, no Kafka), single (single-node, with Kafka) or scale (HA, with Kafka). mesh is not supported in this release"
  type        = string
  validation {
    condition     = contains(["slim", "single", "scale"], var.profile)
    error_message = "profile must be 'slim', 'single' or 'scale' (mesh is not supported in this release; use scale)."
  }

  validation {
    condition     = !(contains(local.store_profiles, var.profile) && local.secrets_backend == "none")
    error_message = "profile ${var.profile} reads the secrets store, so it needs OpenBao: set vault_addr, and the token through VAULT_TOKEN."
  }
}

variable "nfs_server" {
  description = "NFS server hostname"
  type        = string
}

variable "minio_endpoint" {
  description = "MinIO S3-compatible endpoint"
  type        = string
  default     = ""
}

variable "repo_url" {
  description = "Git repo URL for ArgoCD"
  type        = string
}

variable "target_revision" {
  description = "Git branch/tag for ArgoCD"
  type        = string
  default     = "main"
}
