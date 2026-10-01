# Pure unit test — no providers needed.
variables {
  cloud           = "local"
  nfs_server      = "storage.example.com"
  nfs_config_path = "/data/dfe-config"
}

run "local_config_storage" {
  command = plan

  assert {
    condition     = output.config_storage_type == "nfs"
    error_message = "local cloud should use nfs config storage"
  }

  assert {
    condition     = output.config_storage_server == "storage.example.com"
    error_message = "config_storage_server should match nfs_server input"
  }
}

run "aws_config_storage" {
  command = plan

  variables {
    cloud = "aws"
  }

  assert {
    condition     = output.config_storage_type == "s3"
    error_message = "aws cloud should use s3 config storage"
  }
}
