variable "vault_addr" {
  description = "OpenBao server address"
  type        = string
}

variable "vault_token" {
  description = "OpenBao root/admin token for Terraform provisioning (sensitive)"
  type        = string
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
