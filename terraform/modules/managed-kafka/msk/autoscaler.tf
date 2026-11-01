// Broker-count autoscaling. AWS gives MSK Express no equivalent of a scaling
// policy or target-tracking group: UpdateBrokerCount is a manual control-plane
// call, so something has to watch the cluster and call it. That something is a
// Lambda EventBridge runs every var.autoscaling.period_seconds. Each run reads
// the cluster's current broker count, the BytesInPerSec of exactly those
// brokers over the last evaluation_periods, and adds var.autoscaling.step when
// every period's per-broker mean was over the threshold, refusing above
// max_brokers and refusing to ever scale in (broker_scaler.py). It acts only on
// an ACTIVE cluster a full window after its last operation ended, so a run that
// overlaps or repeats another never adds a step on load read before the last one.
//
// A schedule, not a CloudWatch alarm: an alarm names a fixed broker set and
// notifies once per transition, so it never saw an added broker and never fired
// a second step while load stayed high.
//
// No companion check on KafkaDataLogsDiskUsed: Express storage is MSK-managed
// (msk/main.tf carries no storage_info at all), so there is no disk to watch --
// that belongs to a Standard cluster with its own EBS volumes.

locals {
  // AWS's own constraint -- a scale-out step has to keep the broker count a
  // multiple of the client subnet count, exactly like broker_count -- is what
  // makes the subnet count the only default that is always safe.
  autoscaling_step = coalesce(var.autoscaling.step, length(var.network.private_subnet_ids))

  // MB/s is the unit AWS publishes broker network throughput in and the unit
  // BytesInPerSec is reported in. This is the SI-decimal MB -> bytes conversion,
  // not a capacity number, so it is a named local rather than a bare 1000000
  // in the arithmetic below.
  mb_per_s_to_bytes_per_s = 1000000

  // What ONE broker may carry before the cluster needs more of them. Per broker
  // rather than per cluster, so the same figure holds at every broker count.
  autoscaling_threshold_bytes_per_sec_per_broker = (
    var.autoscaling.per_broker_capacity_mb_s
    * local.mb_per_s_to_bytes_per_s
    * var.autoscaling.headroom
  )

  autoscaling_enabled = var.autoscaling.enabled

  // EventBridge's rate() counts whole minutes, and the variable is refused unless it is whole minutes.
  seconds_per_minute         = 60
  autoscaling_period_minutes = var.autoscaling.period_seconds / local.seconds_per_minute
  autoscaling_schedule       = local.autoscaling_period_minutes == 1 ? "rate(1 minute)" : "rate(${local.autoscaling_period_minutes} minutes)"
}

// ---------------------------------------------------------------------------
// The action: a schedule, and a Lambda that calls UpdateBrokerCount
// ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "broker_scaler" {
  count = local.autoscaling_enabled ? 1 : 0

  name                = "${var.name}-msk-broker-scaler"
  description         = "Runs the ${var.name} MSK broker scaler every ${var.autoscaling.period_seconds}s."
  schedule_expression = local.autoscaling_schedule
}

resource "aws_cloudwatch_event_target" "broker_scaler" {
  count = local.autoscaling_enabled ? 1 : 0

  rule = aws_cloudwatch_event_rule.broker_scaler[0].name
  arn  = aws_lambda_function.broker_scaler[0].arn
}

resource "aws_lambda_permission" "broker_scaler_schedule" {
  count = local.autoscaling_enabled ? 1 : 0

  statement_id  = "AllowExecutionFromSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.broker_scaler[0].function_name
  // The EventBridge service principal. Fixed by AWS.
  principal  = "events.amazonaws.com"
  source_arn = aws_cloudwatch_event_rule.broker_scaler[0].arn
}

// DFE's own monitoring on AWS goes to its OTel feed, never CloudWatch -- the
// scaler's metric read is the one deliberate exception, because MSK publishes
// BytesInPerSec to CloudWatch whatever DFE does and UpdateBrokerCount has no
// other signal to act on.
resource "aws_cloudwatch_log_group" "broker_scaler" {
  count = local.autoscaling_enabled ? 1 : 0

  name              = "/aws/lambda/${var.name}-msk-broker-scaler"
  retention_in_days = var.telemetry.forced_retention_days
}

data "aws_iam_policy_document" "broker_scaler_assume" {
  count = local.autoscaling_enabled ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type = "Service"
      // The Lambda service principal. Fixed by AWS.
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "broker_scaler" {
  count = local.autoscaling_enabled ? 1 : 0

  name                 = "${var.name}-msk-broker-scaler"
  path                 = var.iam_path
  assume_role_policy   = data.aws_iam_policy_document.broker_scaler_assume[0].json
  permissions_boundary = var.permissions_boundary
}

// Least privilege: describe and update THIS cluster alone, read its metrics,
// and write to nothing but the log group created above. No AWSLambdaBasicExecutionRole --
// that manages grants logs:CreateLogGroup account-wide, which this role never
// needs because the group already exists.
data "aws_iam_policy_document" "broker_scaler" {
  count = local.autoscaling_enabled ? 1 : 0

  statement {
    effect    = "Allow"
    actions   = ["kafka:DescribeClusterV2", "kafka:ListClusterOperationsV2", "kafka:UpdateBrokerCount"]
    resources = [aws_msk_cluster.this.arn]
  }

  // GetMetricData has no resource-level permissions, so it cannot be scoped to
  // this cluster's metrics. It only reads.
  statement {
    effect    = "Allow"
    actions   = ["cloudwatch:GetMetricData"]
    resources = ["*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.broker_scaler[0].arn}:*"]
  }
}

resource "aws_iam_role_policy" "broker_scaler" {
  count = local.autoscaling_enabled ? 1 : 0

  name   = "${var.name}-msk-broker-scaler"
  role   = aws_iam_role.broker_scaler[0].name
  policy = data.aws_iam_policy_document.broker_scaler[0].json
}

// Stdlib + boto3 only, both of which ship in var.broker_scaler_runtime. The
// handler is its own file so scripts/tests/test_msk_broker_scaler.py runs it.
data "archive_file" "broker_scaler" {
  count = local.autoscaling_enabled ? 1 : 0

  type        = "zip"
  output_path = "${path.module}/.broker-scaler-lambda.zip"
  source_file = "${path.module}/broker_scaler.py"
}

resource "aws_lambda_function" "broker_scaler" {
  count = local.autoscaling_enabled ? 1 : 0

  function_name = "${var.name}-msk-broker-scaler"
  description   = "Adds brokers to ${var.name} while its brokers stay over their throughput. Increase-only -- never scales in."

  role     = aws_iam_role.broker_scaler[0].arn
  filename = data.archive_file.broker_scaler[0].output_path
  // Recomputed whenever broker_scaler.py changes, which is what redeploys the function's code.
  source_code_hash = data.archive_file.broker_scaler[0].output_base64sha256

  handler       = "broker_scaler.handler"
  runtime       = var.broker_scaler_runtime
  architectures = ["arm64"]
  timeout       = 60

  environment {
    variables = {
      CLUSTER_ARN                        = aws_msk_cluster.this.arn
      CLUSTER_NAME                       = aws_msk_cluster.this.cluster_name
      MAX_BROKERS                        = tostring(var.autoscaling.max_brokers)
      STEP                               = tostring(local.autoscaling_step)
      THRESHOLD_BYTES_PER_SEC_PER_BROKER = tostring(local.autoscaling_threshold_bytes_per_sec_per_broker)
      PERIOD_SECONDS                     = tostring(var.autoscaling.period_seconds)
      EVALUATION_PERIODS                 = tostring(var.autoscaling.evaluation_periods)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.broker_scaler,
    aws_iam_role_policy.broker_scaler,
  ]
}
