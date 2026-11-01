// A private, SSE-KMS bucket that either goes with an ephemeral deployment or
// outlives a persistent one's tofu destroy. lifecycle.destroy only takes a
// constant, so every resource comes twice: `ephemeral`, destroyed as usual, and
// `retained`, which destroy forgets. var.retain picks one of each.

data "aws_partition" "current" {}

locals {
  ephemeral_count = var.retain ? 0 : 1
  retained_count  = var.retain ? 1 : 0

  bucket_id = one(concat(aws_s3_bucket.ephemeral[*].bucket, aws_s3_bucket.retained[*].bucket))
  // Built from the name, which S3 ARNs carry verbatim, so a policy can name the bucket before it exists.
  bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::${var.name}"

  // The public-access block refuses a policy or ACL that grants anyone access;
  // this refuses a request that already has access but arrives over plaintext.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Sid       = "DenyInsecureTransport"
          Effect    = "Deny"
          Principal = "*"
          Action    = "s3:*"
          Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
          Condition = { Bool = { "aws:SecureTransport" = "false" } }
        },
      ],
      var.policy_statements,
    )
  })

  versioning_count = var.versioning ? 1 : 0
  lifecycle_count  = length(var.lifecycle_rules) > 0 ? 1 : 0
}

resource "aws_s3_bucket" "ephemeral" {
  count = local.ephemeral_count

  bucket        = var.name
  force_destroy = true

  tags = var.tags
}

resource "aws_s3_bucket" "retained" {
  count = local.retained_count

  bucket        = var.name
  force_destroy = false

  tags = var.tags

  lifecycle {
    destroy = false
  }
}

resource "aws_s3_bucket_public_access_block" "ephemeral" {
  count = local.ephemeral_count

  bucket = local.bucket_id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "retained" {
  count = local.retained_count

  bucket = local.bucket_id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

  lifecycle {
    destroy = false
  }
}

// ACLs off entirely, so no object can carry a grant the policy does not express.
resource "aws_s3_bucket_ownership_controls" "ephemeral" {
  count = local.ephemeral_count

  bucket = local.bucket_id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_ownership_controls" "retained" {
  count = local.retained_count

  bucket = local.bucket_id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }

  lifecycle {
    destroy = false
  }
}

resource "aws_s3_bucket_policy" "ephemeral" {
  count = local.ephemeral_count

  bucket = local.bucket_id
  policy = local.policy
}

resource "aws_s3_bucket_policy" "retained" {
  count = local.retained_count

  bucket = local.bucket_id
  policy = local.policy

  lifecycle {
    destroy = false
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ephemeral" {
  count = local.ephemeral_count

  bucket = local.bucket_id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }

    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "retained" {
  count = local.retained_count

  bucket = local.bucket_id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }

    bucket_key_enabled = true
  }

  lifecycle {
    destroy = false
  }
}

resource "aws_s3_bucket_versioning" "ephemeral" {
  count = local.ephemeral_count * local.versioning_count

  bucket = local.bucket_id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_versioning" "retained" {
  count = local.retained_count * local.versioning_count

  bucket = local.bucket_id

  versioning_configuration {
    status = "Enabled"
  }

  lifecycle {
    destroy = false
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "ephemeral" {
  count = local.ephemeral_count * local.lifecycle_count

  bucket = local.bucket_id

  dynamic "rule" {
    for_each = var.lifecycle_rules

    content {
      id     = rule.value.id
      status = "Enabled"

      filter {}

      dynamic "expiration" {
        for_each = rule.value.expiration_days == null ? [] : [rule.value.expiration_days]

        content {
          days = expiration.value
        }
      }

      dynamic "noncurrent_version_expiration" {
        for_each = rule.value.noncurrent_expiration_days == null ? [] : [rule.value.noncurrent_expiration_days]

        content {
          noncurrent_days = noncurrent_version_expiration.value
        }
      }

      dynamic "abort_incomplete_multipart_upload" {
        for_each = rule.value.abort_multipart_days == null ? [] : [rule.value.abort_multipart_days]

        content {
          days_after_initiation = abort_incomplete_multipart_upload.value
        }
      }
    }
  }

  // S3 applies a lifecycle rule against the versioning state it finds.
  depends_on = [aws_s3_bucket_versioning.ephemeral]
}

resource "aws_s3_bucket_lifecycle_configuration" "retained" {
  count = local.retained_count * local.lifecycle_count

  bucket = local.bucket_id

  dynamic "rule" {
    for_each = var.lifecycle_rules

    content {
      id     = rule.value.id
      status = "Enabled"

      filter {}

      dynamic "expiration" {
        for_each = rule.value.expiration_days == null ? [] : [rule.value.expiration_days]

        content {
          days = expiration.value
        }
      }

      dynamic "noncurrent_version_expiration" {
        for_each = rule.value.noncurrent_expiration_days == null ? [] : [rule.value.noncurrent_expiration_days]

        content {
          noncurrent_days = noncurrent_version_expiration.value
        }
      }

      dynamic "abort_incomplete_multipart_upload" {
        for_each = rule.value.abort_multipart_days == null ? [] : [rule.value.abort_multipart_days]

        content {
          days_after_initiation = abort_incomplete_multipart_upload.value
        }
      }
    }
  }

  depends_on = [aws_s3_bucket_versioning.retained]

  lifecycle {
    destroy = false
  }
}
