output "bucket" {
  description = "The bucket's name."
  value       = local.bucket_id
}

output "arn" {
  description = "The bucket's ARN."
  value       = local.bucket_arn
}

output "retained" {
  description = "Whether tofu destroy forgets this bucket rather than deleting it."
  value       = var.retain
}

output "forgotten_address" {
  description = "The bucket's address inside this module when destroy forgets it, empty otherwise. The caller prefixes its own module path."
  value       = var.retain ? "aws_s3_bucket.retained[0]" : ""
}

output "kms_key_arn" {
  description = "The key the bucket's default encryption names, read back from the encryption configuration."
  value = one([
    for sse in concat(
      aws_s3_bucket_server_side_encryption_configuration.ephemeral,
      aws_s3_bucket_server_side_encryption_configuration.retained,
    ) : one(one(sse.rule).apply_server_side_encryption_by_default).kms_master_key_id
  ])
}
