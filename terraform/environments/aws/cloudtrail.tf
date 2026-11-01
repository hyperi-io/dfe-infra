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

// A persistent deployment's audit trail survives a tofu destroy rather than
// being deleted along with the record of the destroy itself, and so does the key
// it is encrypted under (kubernetes-cluster/aws/kms.tf). Only an ephemeral one
// gets the fast, no-confirmation teardown, the same rule as the secret recovery
// windows above.
//
// Versioning means a principal holding s3:DeleteObject cannot remove a trail
// file with nothing to recover it from, and the noncurrent rule frees what a
// delete leaves. Versioning alone, not Object Lock: Object Lock has to be enabled
// at bucket creation, needs its own retention-period variable, and in compliance
// mode blocks deletion of locked objects outright, which fights an ephemeral
// deployment's teardown. That is a design question for whoever owns the
// tamper-evident-audit story.
//
// The policy is AWS's own documented one for CloudTrail delivery: the ACL check
// plus the write, both scoped to this one trail via aws:SourceArn so a second
// trail elsewhere in the account cannot write here. The bucket's ARN is built
// from its name, because the module's arn is not known before the bucket exists.
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
  s3_bucket_name = module.cloudtrail_bucket[0].bucket

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
  depends_on = [module.cloudtrail_bucket]
}

// aws_cloudtrail.this gained `count` for cloudtrail.enabled. Without this a
// deployment applied before it reads its trail as one destroy and one create.

moved {
  from = aws_cloudtrail.this
  to   = aws_cloudtrail.this[0]
}
