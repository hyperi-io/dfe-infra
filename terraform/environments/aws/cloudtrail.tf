// Account-level, not module-shaped: CloudTrail records everything that
// happens to the deployment, including the modules above, so it belongs to
// the root rather than to any one of them. Management events only, under
// BOTH telemetry sinks -- unlike MSK's broker logs there is no S3-vs-otel
// choice at this layer, since a trail always delivers to S3 regardless of
// var.telemetry.sink. What the dial actually gates is the CloudWatch Logs
// attachment, the AWS-native half that sink = cloudwatch opts into.

data "aws_partition" "current" {}

locals {
  cloudtrail_name = "${var.name}-cloudtrail"
  // The trail's own ARN is needed by the bucket policy below BEFORE the trail
  // exists -- CreateTrail validates the bucket policy already grants it
  // access, so the policy cannot depend on the resource it authorises.
  // Constructing it from account, region and name breaks that cycle.
  cloudtrail_arn = "arn:${data.aws_partition.current.partition}:cloudtrail:${var.provision.region}:${data.aws_caller_identity.current.account_id}:trail/${local.cloudtrail_name}"

  // var.cloudtrail.enabled gates everything in this file; the sink gates the
  // CloudWatch half inside it.
  cloudtrail_count            = var.cloudtrail.enabled ? 1 : 0
  cloudtrail_cloudwatch_count = var.cloudtrail.enabled && var.telemetry.sink == "cloudwatch" ? 1 : 0
}

resource "aws_s3_bucket" "cloudtrail" {
  count = local.cloudtrail_count

  bucket = "${var.s3_bucket_prefix}${local.cloudtrail_name}"

  // Same rule as the KMS deletion window (kubernetes-cluster/aws/kms.tf) and
  // the secret recovery windows above: only an ephemeral deployment gets the
  // fast, no-confirmation teardown. A persistent deployment's audit trail
  // survives a tofu destroy rather than being deleted along with the record
  // of the destroy itself.
  force_destroy = local.ephemeral

  tags = { Name = local.cloudtrail_name }
}

resource "aws_s3_bucket_public_access_block" "cloudtrail" {
  count = local.cloudtrail_count

  bucket = aws_s3_bucket.cloudtrail[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

// Without this, a principal holding s3:DeleteObject on the bucket (not just
// s3:PutObject) can remove individual trail files with nothing to recover
// them from. Versioning alone, not Object Lock: Object Lock has to be
// enabled at bucket creation, needs its own retention-period variable, and in
// compliance mode blocks deletion of locked objects outright -- which fights
// force_destroy on an ephemeral deployment's teardown. That is a deliberate
// design question for whoever owns the tamper-evident-audit story, not a
// same-shaped fix as this one.
resource "aws_s3_bucket_versioning" "cloudtrail" {
  count = local.cloudtrail_count

  bucket = aws_s3_bucket.cloudtrail[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cloudtrail" {
  count = local.cloudtrail_count

  bucket = aws_s3_bucket.cloudtrail[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = module.cluster.kms_key_arn
    }

    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "cloudtrail" {
  count = local.cloudtrail_count

  bucket = aws_s3_bucket.cloudtrail[0].id

  rule {
    id     = "expire-cloudtrail-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.telemetry.retention_days
    }

    // Versioning above means a delete no longer removes the object -- it
    // becomes a noncurrent version. With no rule for THOSE, a bucket with
    // s3:DeleteObject exercised against it (accidentally or not) grows
    // forever instead of the delete actually freeing anything.
    noncurrent_version_expiration {
      noncurrent_days = var.telemetry.retention_days
    }
  }
}

// AWS's own documented bucket policy for CloudTrail delivery: the ACL check
// plus the write, both scoped to this one trail via aws:SourceArn so a
// second trail elsewhere in the account cannot write here.
data "aws_iam_policy_document" "cloudtrail_bucket" {
  count = local.cloudtrail_count

  statement {
    sid    = "AWSCloudTrailAclCheck20150319"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.cloudtrail[0].arn]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.cloudtrail_arn]
    }
  }

  statement {
    sid    = "AWSCloudTrailWrite20150319"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.cloudtrail[0].arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.cloudtrail_arn]
    }
  }
}

resource "aws_s3_bucket_policy" "cloudtrail" {
  count = local.cloudtrail_count

  bucket = aws_s3_bucket.cloudtrail[0].id
  policy = data.aws_iam_policy_document.cloudtrail_bucket[0].json
}

// ---------------------------------------------------------------------------
// cloudwatch sink only: the AWS-native half. otel builds none of this --
// CloudTrail's events already reach the OTel feed some other way once the
// fetcher is wired to the S3 bucket above, so a duplicate CloudWatch copy
// would be pure cost with nothing new to read it.
// ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "cloudtrail" {
  count = local.cloudtrail_cloudwatch_count

  name              = "/aws/cloudtrail/${local.cloudtrail_name}"
  retention_in_days = var.telemetry.retention_days
}

data "aws_iam_policy_document" "cloudtrail_assume" {
  count = local.cloudtrail_cloudwatch_count

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cloudtrail_cloudwatch" {
  count = local.cloudtrail_cloudwatch_count

  name                 = "${var.name}-cloudtrail-cloudwatch"
  path                 = var.iam_path
  assume_role_policy   = data.aws_iam_policy_document.cloudtrail_assume[0].json
  permissions_boundary = var.permissions_boundary
}

// AWS's own documented role policy for CloudTrail-to-CloudWatch-Logs delivery.
data "aws_iam_policy_document" "cloudtrail_cloudwatch" {
  count = local.cloudtrail_cloudwatch_count

  statement {
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = ["${aws_cloudwatch_log_group.cloudtrail[0].arn}:*"]
  }
}

resource "aws_iam_role_policy" "cloudtrail_cloudwatch" {
  count = local.cloudtrail_cloudwatch_count

  name   = "${var.name}-cloudtrail-cloudwatch"
  role   = aws_iam_role.cloudtrail_cloudwatch[0].name
  policy = data.aws_iam_policy_document.cloudtrail_cloudwatch[0].json
}

resource "aws_cloudtrail" "this" {
  count = local.cloudtrail_count

  name           = local.cloudtrail_name
  s3_bucket_name = aws_s3_bucket.cloudtrail[0].id

  include_global_service_events = true
  is_multi_region_trail         = false
  enable_log_file_validation    = true

  // No data_resource block: management events only, everywhere this trail
  // is asked for.
  event_selector {
    read_write_type           = "All"
    include_management_events = true
  }

  cloud_watch_logs_group_arn = local.cloudtrail_cloudwatch_count == 1 ? "${aws_cloudwatch_log_group.cloudtrail[0].arn}:*" : null
  cloud_watch_logs_role_arn  = local.cloudtrail_cloudwatch_count == 1 ? aws_iam_role.cloudtrail_cloudwatch[0].arn : null

  // The bucket policy has to exist before CreateTrail validates against it.
  depends_on = [aws_s3_bucket_policy.cloudtrail]
}

// The resources above gained `count` for cloudtrail.enabled. Without these a
// deployment applied before it reads its trail as one destroy and one create,
// and the destroy takes the audit bucket's history with it. moved.tf holds the
// edge module's move alone, which scripts/tests/test_edge_moved_blocks.py asserts.

moved {
  from = aws_s3_bucket.cloudtrail
  to   = aws_s3_bucket.cloudtrail[0]
}

moved {
  from = aws_s3_bucket_public_access_block.cloudtrail
  to   = aws_s3_bucket_public_access_block.cloudtrail[0]
}

moved {
  from = aws_s3_bucket_versioning.cloudtrail
  to   = aws_s3_bucket_versioning.cloudtrail[0]
}

moved {
  from = aws_s3_bucket_server_side_encryption_configuration.cloudtrail
  to   = aws_s3_bucket_server_side_encryption_configuration.cloudtrail[0]
}

moved {
  from = aws_s3_bucket_lifecycle_configuration.cloudtrail
  to   = aws_s3_bucket_lifecycle_configuration.cloudtrail[0]
}

moved {
  from = aws_s3_bucket_policy.cloudtrail
  to   = aws_s3_bucket_policy.cloudtrail[0]
}

moved {
  from = aws_cloudtrail.this
  to   = aws_cloudtrail.this[0]
}
