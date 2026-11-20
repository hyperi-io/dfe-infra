// Broker-count autoscaling: an EventBridge schedule runs a Lambda that calls UpdateBrokerCount, which Express has no scaling policy for.
// How a run decides, and why a schedule rather than an alarm: docs/deployment/kafka/aws-msk-operations.md#broker-count-autoscaling.

locals {
  // A step must keep the broker count a multiple of the subnet count, so the subnet count is the safe default.
  autoscaling_step = coalesce(var.autoscaling.step, length(var.network.private_subnet_ids))

  mb_per_s_to_bytes_per_s = 1000000

  autoscaling_threshold_bytes_per_sec_per_broker = (
    var.autoscaling.per_broker_capacity_mb_s
    * local.mb_per_s_to_bytes_per_s
    * var.autoscaling.headroom
  )

  autoscaling_enabled = var.autoscaling.enabled

  // rate() counts whole minutes, and variables.tf enforces them.
  seconds_per_minute         = 60
  autoscaling_period_minutes = var.autoscaling.period_seconds / local.seconds_per_minute
  autoscaling_schedule       = local.autoscaling_period_minutes == 1 ? "rate(1 minute)" : "rate(${local.autoscaling_period_minutes} minutes)"
}

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
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.broker_scaler[0].arn
}

// The scaler's log group is CloudWatch under either telemetry sink.
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
      type        = "Service"
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

// Least privilege: this cluster, and the one log group. No AWSLambdaBasicExecutionRole, which grants logs:CreateLogGroup account-wide.
data "aws_iam_policy_document" "broker_scaler" {
  count = local.autoscaling_enabled ? 1 : 0

  statement {
    effect    = "Allow"
    actions   = ["kafka:DescribeClusterV2", "kafka:ListClusterOperationsV2", "kafka:UpdateBrokerCount"]
    resources = [aws_msk_cluster.this.arn]
  }

  // GetMetricData has no resource-level permissions.
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

// Stdlib and boto3 only, both in var.broker_scaler_runtime. scripts/tests/test_msk_broker_scaler.py runs the handler.
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
  // Redeploys the code when broker_scaler.py changes.
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
