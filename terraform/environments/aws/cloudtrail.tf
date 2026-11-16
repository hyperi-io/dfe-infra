// The deployment's CloudTrail trail, gated by var.cloudtrail.enabled (docs/deployment/aws-configuration.md#what-every-deployment-builds).

data "aws_partition" "current" {}

locals {
  cloudtrail_name = "${var.name}-cloudtrail"
  // Built from its parts because CreateTrail checks the bucket policy already grants the trail, so the policy can't reference the resource.
  cloudtrail_arn = "arn:${data.aws_partition.current.partition}:cloudtrail:${var.provision.region}:${data.aws_caller_identity.current.account_id}:trail/${local.cloudtrail_name}"

  cloudtrail_count            = var.cloudtrail.enabled ? 1 : 0
  cloudtrail_cloudwatch_count = var.cloudtrail.enabled ? 1 : 0
  cloudtrail_log_retention    = var.telemetry.sink == "otel" ? var.telemetry.forced_retention_days : var.telemetry.retention_days
}

// The bucket's ARN is built from its name because the module's arn is unknown before the bucket exists.
locals {
  cloudtrail_bucket_name = "${var.s3_bucket_prefix}${local.cloudtrail_name}"
  cloudtrail_bucket_arn  = "arn:${data.aws_partition.current.partition}:s3:::${local.cloudtrail_bucket_name}"
}

module "cloudtrail_bucket" {
  count = local.cloudtrail_count

  source = "../../modules/s3-bucket"

  name        = local.cloudtrail_bucket_name
  retain      = !local.ephemeral
  kms_key_arn = module.cluster.kms_key_arn
  versioning  = true

  lifecycle_rules = [
    {
      id                         = "expire-cloudtrail-logs"
      expiration_days            = var.telemetry.retention_days
      noncurrent_expiration_days = var.telemetry.retention_days
    },
  ]

  policy_statements = [
    {
      Sid       = "AWSCloudTrailAclCheck20150319"
      Effect    = "Allow"
      Principal = { Service = "cloudtrail.amazonaws.com" }
      Action    = "s3:GetBucketAcl"
      Resource  = local.cloudtrail_bucket_arn
      Condition = { StringEquals = { "aws:SourceArn" = local.cloudtrail_arn } }
    },
    {
      Sid       = "AWSCloudTrailWrite20150319"
      Effect    = "Allow"
      Principal = { Service = "cloudtrail.amazonaws.com" }
      Action    = "s3:PutObject"
      Resource  = "${local.cloudtrail_bucket_arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
      Condition = {
        StringEquals = {
          "s3:x-amz-acl"  = "bucket-owner-full-control"
          "aws:SourceArn" = local.cloudtrail_arn
        }
      }
    },
  ]

  tags = { Name = local.cloudtrail_name }
}

// The CloudWatch Logs half.

resource "aws_cloudwatch_log_group" "cloudtrail" {
  count = local.cloudtrail_cloudwatch_count

  name              = "/aws/cloudtrail/${local.cloudtrail_name}"
  retention_in_days = local.cloudtrail_log_retention
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
  s3_bucket_name = module.cloudtrail_bucket[0].bucket

  include_global_service_events = true
  is_multi_region_trail         = false
  enable_log_file_validation    = true

  event_selector {
    read_write_type           = "All"
    include_management_events = true
  }

  cloud_watch_logs_group_arn = local.cloudtrail_cloudwatch_count == 1 ? "${aws_cloudwatch_log_group.cloudtrail[0].arn}:*" : null
  cloud_watch_logs_role_arn  = local.cloudtrail_cloudwatch_count == 1 ? aws_iam_role.cloudtrail_cloudwatch[0].arn : null

  // CreateTrail validates the bucket policy and that CloudTrail can write to the log group, so both must exist first.
  depends_on = [module.cloudtrail_bucket, aws_iam_role_policy.cloudtrail_cloudwatch]
}

moved {
  from = aws_cloudtrail.this
  to   = aws_cloudtrail.this[0]
}
