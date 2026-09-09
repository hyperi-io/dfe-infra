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
  description = "Deployment profile: slim (single-node, gRPC, no Kafka), single (single-node, with Kafka), scale (HA, with Kafka) or scale-mesh (HA, gRPC, no Kafka)"
  type        = string
  default     = "slim"
  validation {
    condition     = contains(["slim", "single", "scale", "scale-mesh"], var.profile)
    error_message = "profile must be 'slim', 'single', 'scale' or 'scale-mesh'."
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

variable "registry_host" {
  description = "Container registry hostname (JFrog)"
  type        = string
  default     = ""
}

variable "registry_user" {
  description = "Container registry username"
  type        = string
  default     = ""
}

variable "registry_token" {
  description = "Container registry token (sensitive)"
  type        = string
  sensitive   = true
  default     = ""
}
