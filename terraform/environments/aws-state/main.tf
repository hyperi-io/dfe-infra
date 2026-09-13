// The one-shot that has to run before any other AWS root can init: the bucket
// their state lives in. Run once per account.

data "aws_caller_identity" "current" {}

locals {
  // s3- is the resource-type prefix; the organisation prefix in front of it is
  // what keeps a globally-unique name from colliding with someone else's.
  bucket = var.bucket_name != "" ? var.bucket_name : "${var.bucket_prefix}-s3-${var.name}-tfstate"
}

resource "terraform_data" "account_guard" {
  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.account
      error_message = "This shell is authenticated to AWS account ${data.aws_caller_identity.current.account_id}, but account is set to ${var.account}. Re-authenticate, or correct the variable."
    }
  }
}

resource "aws_s3_bucket" "state" {
  bucket = local.bucket

  // Deleting this bucket deletes the record of every resource every other root
  // owns, and they become invisible rather than destroyed.
  lifecycle {
    prevent_destroy = true
  }

  depends_on = [terraform_data.account_guard]
}

// State is overwritten on every apply. Versioning is what turns a bad apply,
// or a lock that was broken by hand, into a recoverable mistake.
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

// State routinely holds secrets in plaintext -- generated passwords, tokens,
// certificate keys -- so the bucket encrypts by default rather than trusting
// every writer to ask for it.
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.kms_key_arn == "" ? "AES256" : "aws:kms"
      kms_master_key_id = var.kms_key_arn == "" ? null : var.kms_key_arn
    }

    // Cuts KMS request charges on a bucket that is read and written on every
    // plan. No effect under SSE-S3.
    bucket_key_enabled = var.kms_key_arn != ""
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
