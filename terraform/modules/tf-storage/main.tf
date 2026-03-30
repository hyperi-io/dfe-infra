# For local: no resources — NFS and MinIO are pre-existing.
# For cloud: would create S3/GCS/Azure Blob buckets (not yet implemented).

locals {
  config_storage = {
    local = {
      type   = "nfs"
      server = var.nfs_server
      path   = var.nfs_config_path
    }
    aws = {
      type   = "s3"
      server = ""
      path   = ""
    }
    gcp = {
      type   = "gcs"
      server = ""
      path   = ""
    }
    az = {
      type   = "azurefiles"
      server = ""
      path   = ""
    }
  }
}
