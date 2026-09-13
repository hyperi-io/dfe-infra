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
}

resource "aws_s3_bucket" "cloudtrail" {
  bucket        = local.cloudtrail_name
  force_destroy = true

  tags = { Name = local.cloudtrail_name }
}

resource "aws_s3_bucket_public_access_block" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = module.cluster.kms_key_arn
    }

    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  rule {
    id     = "expire-cloudtrail-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.telemetry.retention_days
    }
  }
}

// AWS's own documented bucket policy for CloudTrail delivery: the ACL check
// plus the write, both scoped to this one trail via aws:SourceArn so a
// second trail elsewhere in the account cannot write here.
data "aws_iam_policy_document" "cloudtrail_bucket" {
  statement {
    sid    = "AWSCloudTrailAclCheck20150319"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.cloudtrail.arn]

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
    resources = ["${aws_s3_bucket.cloudtrail.arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]

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
  bucket = aws_s3_bucket.cloudtrail.id
  policy = data.aws_iam_policy_document.cloudtrail_bucket.json
}

// ---------------------------------------------------------------------------
// cloudwatch sink only: the AWS-native half. otel builds none of this --
// CloudTrail's events already reach the OTel feed some other way once the
// fetcher is wired to the S3 bucket above, so a duplicate CloudWatch copy
// would be pure cost with nothing new to read it.
// ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "cloudtrail" {
  count = var.telemetry.sink == "cloudwatch" ? 1 : 0

  name              = "/aws/cloudtrail/${local.cloudtrail_name}"
  retention_in_days = var.telemetry.retention_days
}

data "aws_iam_policy_document" "cloudtrail_assume" {
  count = var.telemetry.sink == "cloudwatch" ? 1 : 0

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
  count = var.telemetry.sink == "cloudwatch" ? 1 : 0

  name               = "${var.name}-cloudtrail-cloudwatch"
  assume_role_policy = data.aws_iam_policy_document.cloudtrail_assume[0].json
}

// AWS's own documented role policy for CloudTrail-to-CloudWatch-Logs delivery.
data "aws_iam_policy_document" "cloudtrail_cloudwatch" {
  count = var.telemetry.sink == "cloudwatch" ? 1 : 0

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
  count = var.telemetry.sink == "cloudwatch" ? 1 : 0

  name   = "${var.name}-cloudtrail-cloudwatch"
  role   = aws_iam_role.cloudtrail_cloudwatch[0].name
  policy = data.aws_iam_policy_document.cloudtrail_cloudwatch[0].json
}

resource "aws_cloudtrail" "this" {
  name           = local.cloudtrail_name
  s3_bucket_name = aws_s3_bucket.cloudtrail.id

  include_global_service_events = true
  is_multi_region_trail         = false
  enable_log_file_validation    = true

  // No data_resource block: management events only, everywhere this trail
  // is asked for.
  event_selector {
    read_write_type           = "All"
    include_management_events = true
  }

  cloud_watch_logs_group_arn = var.telemetry.sink == "cloudwatch" ? "${aws_cloudwatch_log_group.cloudtrail[0].arn}:*" : null
  cloud_watch_logs_role_arn  = var.telemetry.sink == "cloudwatch" ? aws_iam_role.cloudtrail_cloudwatch[0].arn : null

  // The bucket policy has to exist before CreateTrail validates against it.
  depends_on = [aws_s3_bucket_policy.cloudtrail]
}
