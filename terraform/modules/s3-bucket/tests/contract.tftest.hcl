// Provider-free: mock_provider means no credentials, no API call and no cost.

mock_provider "aws" {
  // The partition is half of the bucket's ARN.
  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }
}

variables {
  name        = "dfe-contract-example-000000000000"
  retain      = false
  kms_key_arn = "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000"
  versioning  = true

  lifecycle_rules = [
    { id = "expire", expiration_days = 30, noncurrent_expiration_days = 30 },
    { id = "abort", abort_multipart_days = 7 },
  ]

  policy_statements = [
    {
      Sid       = "AllowMockService"
      Effect    = "Allow"
      Principal = { Service = "mock.amazonaws.com" }
      Action    = "s3:PutObject"
      Resource  = "arn:aws:s3:::dfe-contract-example-000000000000/*"
    },
  ]

  tags = { Name = "dfe-contract-example" }
}

// An ephemeral deployment's bucket goes with its objects, and nothing is forgotten.
run "an_ephemeral_bucket_is_force_destroyed" {
  command = plan

  assert {
    condition     = length(aws_s3_bucket.ephemeral) == 1 && length(aws_s3_bucket.retained) == 0
    error_message = "retain = false must build the bucket a destroy deletes"
  }

  assert {
    condition     = aws_s3_bucket.ephemeral[0].force_destroy == true
    error_message = "an ephemeral bucket is force-destroyed, objects and all"
  }

  assert {
    condition = alltrue([
      length(aws_s3_bucket_public_access_block.retained) == 0,
      length(aws_s3_bucket_ownership_controls.retained) == 0,
      length(aws_s3_bucket_policy.retained) == 0,
      length(aws_s3_bucket_server_side_encryption_configuration.retained) == 0,
      length(aws_s3_bucket_versioning.retained) == 0,
      length(aws_s3_bucket_lifecycle_configuration.retained) == 0,
    ])
    error_message = "no resource of an ephemeral bucket may be one a destroy forgets"
  }

  assert {
    condition     = output.retained == false && output.bucket == var.name
    error_message = "retained and bucket report the bucket this call built"
  }
}

// A persistent deployment's bucket, and the configuration that keeps it private
// and encrypted, all survive a destroy. scripts/tests/test_retained_on_destroy.py
// reads the lifecycle blocks, which are not resource attributes.
run "a_retained_bucket_keeps_every_resource" {
  command = plan

  variables {
    retain = true
  }

  assert {
    condition     = length(aws_s3_bucket.retained) == 1 && length(aws_s3_bucket.ephemeral) == 0
    error_message = "retain = true must build the bucket a destroy forgets"
  }

  assert {
    condition     = aws_s3_bucket.retained[0].force_destroy == false
    error_message = "a retained bucket never force-destroys its objects"
  }

  assert {
    condition = alltrue([
      length(aws_s3_bucket_public_access_block.ephemeral) == 0,
      length(aws_s3_bucket_ownership_controls.ephemeral) == 0,
      length(aws_s3_bucket_policy.ephemeral) == 0,
      length(aws_s3_bucket_server_side_encryption_configuration.ephemeral) == 0,
      length(aws_s3_bucket_versioning.ephemeral) == 0,
      length(aws_s3_bucket_lifecycle_configuration.ephemeral) == 0,
      length(aws_s3_bucket_public_access_block.retained) == 1,
      length(aws_s3_bucket_server_side_encryption_configuration.retained) == 1,
    ])
    error_message = "every resource of a retained bucket is one a destroy forgets, or the bucket outlives its public-access block and encryption"
  }

  assert {
    condition     = output.retained == true && output.arn == "arn:aws:s3:::dfe-contract-example-000000000000"
    error_message = "the outputs report the retained bucket"
  }
}

run "the_bucket_is_private_and_encrypted" {
  command = plan

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.ephemeral[0].block_public_acls,
      aws_s3_bucket_public_access_block.ephemeral[0].block_public_policy,
      aws_s3_bucket_public_access_block.ephemeral[0].ignore_public_acls,
      aws_s3_bucket_public_access_block.ephemeral[0].restrict_public_buckets,
    ])
    error_message = "every public-access setting must be on"
  }

  assert {
    condition     = one(aws_s3_bucket_ownership_controls.ephemeral[0].rule).object_ownership == "BucketOwnerEnforced"
    error_message = "ACLs are off outright"
  }

  // rule and apply_server_side_encryption_by_default are sets, so one() reaches inside.
  assert {
    condition = (
      one(one(aws_s3_bucket_server_side_encryption_configuration.ephemeral[0].rule).apply_server_side_encryption_by_default).sse_algorithm == "aws:kms"
      && one(one(aws_s3_bucket_server_side_encryption_configuration.ephemeral[0].rule).apply_server_side_encryption_by_default).kms_master_key_id == var.kms_key_arn
      && one(aws_s3_bucket_server_side_encryption_configuration.ephemeral[0].rule).bucket_key_enabled
    )
    error_message = "objects are SSE-KMS under the caller's key, with the bucket key on"
  }

  assert {
    condition     = [for s in jsondecode(aws_s3_bucket_policy.ephemeral[0].policy).Statement : s.Condition.Bool["aws:SecureTransport"] if s.Sid == "DenyInsecureTransport"][0] == "false"
    error_message = "the bucket refuses a request over plaintext HTTP"
  }

  assert {
    condition     = [for s in jsondecode(aws_s3_bucket_policy.ephemeral[0].policy).Statement : s.Effect if s.Sid == "AllowMockService"][0] == "Allow"
    error_message = "a caller's statement reaches the policy beside the deny"
  }
}

run "versioning_and_lifecycle_follow_the_caller" {
  command = plan

  assert {
    condition     = one(aws_s3_bucket_versioning.ephemeral[0].versioning_configuration).status == "Enabled"
    error_message = "versioning = true enables versioning"
  }

  assert {
    condition = (
      aws_s3_bucket_lifecycle_configuration.ephemeral[0].rule[0].expiration[0].days == 30
      && aws_s3_bucket_lifecycle_configuration.ephemeral[0].rule[0].noncurrent_version_expiration[0].noncurrent_days == 30
      && aws_s3_bucket_lifecycle_configuration.ephemeral[0].rule[1].abort_incomplete_multipart_upload[0].days_after_initiation == 7
    )
    error_message = "each rule carries exactly the days the caller set"
  }

  // A Disabled rule, or one with no filter block, matches no object.
  assert {
    condition     = alltrue([for r in aws_s3_bucket_lifecycle_configuration.ephemeral[0].rule : r.status == "Enabled" && length(r.filter) == 1])
    error_message = "every rule is Enabled with its empty filter"
  }

  assert {
    condition     = length(aws_s3_bucket_lifecycle_configuration.ephemeral[0].rule[1].expiration) == 0
    error_message = "a rule with no expiration_days expires nothing"
  }
}

run "no_versioning_and_no_rules_build_neither" {
  command = plan

  variables {
    versioning      = false
    lifecycle_rules = []
  }

  assert {
    condition     = length(aws_s3_bucket_versioning.ephemeral) == 0 && length(aws_s3_bucket_lifecycle_configuration.ephemeral) == 0
    error_message = "an unversioned bucket with no rules carries neither resource"
  }
}
