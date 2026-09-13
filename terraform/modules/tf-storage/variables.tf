variable "cloud" {
  description = "Target cloud (local/aws/gcp/azure)"
  type        = string
}

variable "nfs_server" {
  description = "NFS server hostname (local target)"
  type        = string
  default     = ""
}

variable "nfs_config_path" {
  description = "NFS export path for DFE config storage"
  type        = string
  default     = "/data/dfe-config"
}

variable "nfs_backup_path" {
  description = "NFS export path for backups"
  type        = string
  default     = "/data/dfe-backups"
}

variable "minio_endpoint" {
  description = "MinIO S3-compatible endpoint (local target)"
  type        = string
  default     = ""
}
