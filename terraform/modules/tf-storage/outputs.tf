output "config_storage_type" {
  description = "Config storage backend type (nfs/s3/gcs/azurefiles)"
  value       = local.config_storage[var.cloud].type
}

output "config_storage_server" {
  description = "Config storage server (NFS hostname or empty for cloud)"
  value       = local.config_storage[var.cloud].server
}

output "config_storage_path" {
  description = "Config storage path (NFS export path or empty for cloud)"
  value       = local.config_storage[var.cloud].path
}

output "backup_endpoint" {
  description = "Backup storage endpoint (MinIO for local, S3/GCS/Azure for cloud)"
  value       = var.cloud == "local" ? var.minio_endpoint : ""
}
