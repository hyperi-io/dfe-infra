// Broker-count autoscaling. AWS gives MSK Express no equivalent of a scaling
// policy or target-tracking group: UpdateBrokerCount is a manual control-plane
// call, so something has to watch the cluster and call it. That something is a
// CloudWatch alarm on cluster-wide BytesInPerSec, an SNS topic it notifies, and
// a Lambda that reads the current broker count and adds var.autoscaling.step,
// refusing above max_brokers and refusing to ever scale in.
//
// No companion alarm on KafkaDataLogsDiskUsed: Express storage is MSK-managed
// (msk/main.tf carries no storage_info at all), so there is no disk to watch --
// that alarm belongs to a Standard cluster with its own EBS volumes.
//
// Hysteresis is the alarm's own OK/ALARM cycle, nothing more. datapoints_to_alarm
// = evaluation_periods = 3 at a 300s period is the 15-minute stabilisation
// window; the alarm will not raise SNS again until BytesInPerSec drops back
// under threshold and climbs past it a second time, so the Lambda never needs
// its own cooldown or re-arm logic.

locals {
  // AWS's own constraint -- a scale-out step has to keep the broker count a
  // multiple of the client subnet count, exactly like broker_count -- is what
  // makes the subnet count the only default that is always safe.
  autoscaling_step = coalesce(var.autoscaling.step, length(var.network.private_subnet_ids))

  // MB/s is the unit AWS publishes broker network throughput in and the unit
  // BytesInPerSec is reported in; this is the SI-decimal MB -> bytes conversion,
  // not a capacity number, so it is a named local rather than a bare 1000000
  // in the arithmetic below.
  mb_per_s_to_bytes_per_s = 1000000

  // The cluster is scaled out once its combined broker throughput sustains
  // above a HEADROOM-scaled reading of what the current broker count can carry.
  // broker_count, per_broker_capacity_mb_s and headroom are all module
  // variables -- the root fills them from the dial and the resolved shape,
  // never a literal here.
  autoscaling_threshold_bytes_per_sec = (
    var.broker_count
    * var.autoscaling.per_broker_capacity_mb_s
    * local.mb_per_s_to_bytes_per_s
    * var.autoscaling.headroom
  )

  autoscaling_enabled = var.autoscaling.enabled
}

// ---------------------------------------------------------------------------
// The signal: cluster-wide BytesInPerSec vs the sized threshold
// ---------------------------------------------------------------------------

// DFE's own monitoring on AWS goes to its OTel feed, never CloudWatch -- this
// alarm is the one deliberate exception, and the only CloudWatch touchpoint
// this scaler adds. It stays because MSK publishes BytesInPerSec to
// CloudWatch regardless of what DFE does with it, and one alarm costs about
// USD 0.10/month, which buys the only signal AWS exposes for a manual API.
resource "aws_cloudwatch_metric_alarm" "broker_scale_out" {
  count = local.autoscaling_enabled ? 1 : 0

  alarm_name        = "${var.name}-msk-broker-scale-out"
  alarm_description = "Cluster-wide BytesInPerSec on ${var.name} has stayed above ${local.autoscaling_threshold_bytes_per_sec} B/s for 3 of 3 five-minute periods -- add ${local.autoscaling_step} broker(s), up to ${var.autoscaling.max_brokers}."

  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = local.autoscaling_threshold_bytes_per_sec
  // A missing datapoint means the cluster is idle or CloudWatch has not
  // caught up, neither of which is a reason to scale out.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.broker_scaler[0].arn]

  // AWS/Kafka publishes BytesInPerSec per broker (dimensions "Cluster Name",
  // "Broker ID"), never as a cluster total, and the broker count itself is
  // what this alarm exists to change -- so the set of dimension values it
  // sums over cannot be named in advance. SEARCH is what re-discovers every
  // current broker's metric stream each evaluation, which a fixed list of
  // dimensions could not survive a scale-out of its own making.
  //
  // The statistic SEARCH asks for is 'Average', not 'Sum': BytesInPerSec is
  // already a per-second RATE, published once a minute, so a 300s period
  // holds 5 of those one-minute rate samples. Sum is defined as "the sum of
  // the values of all data points collected during the period" (CloudWatch
  // statistics docs), so summing 5 already-averaged rate samples yields a
  // number on the order of 5x the true sustained rate -- comparing THAT
  // against a bytes-per-second threshold fired the alarm at roughly a fifth
  // of the traffic it was sized for. Average is Sum/SampleCount, which
  // collapses the 5 samples back to the mean rate over the period regardless
  // of how many landed in it. The outer SUM() is unchanged and is not the
  // same kind of sum: it is metric-math's SPATIAL aggregation across the
  // broker dimension SEARCH returns, adding per-broker rates together at each
  // timestamp to get a cluster-wide rate -- which is dimensionally correct
  // the way summing 5 temporal samples of one rate is not.
  // https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Statistics-definitions.html
  metric_query {
    id         = "cluster_bytes_in"
    expression = "SUM(SEARCH('{AWS/Kafka,\"Broker ID\",\"Cluster Name\"} MetricName=\"BytesInPerSec\" \"Cluster Name\"=\"${aws_msk_cluster.this.cluster_name}\"', 'Average', 300))"
    label      = "${var.name} cluster BytesInPerSec"
    // CloudWatch's PutMetricAlarm rejects a math-only metric_query with no
    // period, even though the SEARCH expression above already embeds 300.
    period      = 300
    return_data = true
  }
}

// ---------------------------------------------------------------------------
// The action: an SNS topic, a Lambda that calls UpdateBrokerCount
// ---------------------------------------------------------------------------

resource "aws_sns_topic" "broker_scaler" {
  count = local.autoscaling_enabled ? 1 : 0

  name = "${var.name}-msk-broker-scaler"
}

// DFE's own monitoring on AWS goes to its OTel feed, never CloudWatch, so a
// CloudWatch log group a managed service forces into existence stays
// short-retention and interim rather than the Lambda default of never expire.
resource "aws_cloudwatch_log_group" "broker_scaler" {
  count = local.autoscaling_enabled ? 1 : 0

  name              = "/aws/lambda/${var.name}-msk-broker-scaler"
  retention_in_days = 1
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

  name               = "${var.name}-msk-broker-scaler"
  assume_role_policy = data.aws_iam_policy_document.broker_scaler_assume[0].json
}

// Least privilege: describe and update THIS cluster alone, and write to
// nothing but the log group created above. No AWSLambdaBasicExecutionRole --
// that manages grants logs:CreateLogGroup account-wide, which this role never
// needs because the group already exists.
data "aws_iam_policy_document" "broker_scaler" {
  count = local.autoscaling_enabled ? 1 : 0

  statement {
    effect    = "Allow"
    actions   = ["kafka:DescribeClusterV2", "kafka:UpdateBrokerCount"]
    resources = [aws_msk_cluster.this.arn]
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

// Stdlib + boto3 only (both ship in the python3.13 runtime), so the code
// lives inline here rather than as a file the repo has to track and package
// separately.
data "archive_file" "broker_scaler" {
  count = local.autoscaling_enabled ? 1 : 0

  type        = "zip"
  output_path = "${path.module}/.broker-scaler-lambda.zip"

  source_content_filename = "handler.py"
  source_content          = <<-PYTHON
    """Add STEP brokers to an MSK Express cluster, and never take any away.

    Triggered by the scale-out alarm's SNS topic. The message body is not
    read -- this always looks up the cluster's OWN current state before
    acting, which is what makes a retried or duplicate SNS delivery safe.
    """
    import logging
    import os

    import boto3

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    kafka = boto3.client("kafka")


    def handler(event, context):
        cluster_arn = os.environ["CLUSTER_ARN"]
        max_brokers = int(os.environ["MAX_BROKERS"])
        step = int(os.environ["STEP"])

        described = kafka.describe_cluster_v2(ClusterArn=cluster_arn)
        cluster_info = described["ClusterInfo"]
        current_version = cluster_info["CurrentVersion"]
        current_brokers = cluster_info["Provisioned"]["NumberOfBrokerNodes"]

        target_brokers = current_brokers + step

        # Increase-only, by construction: nothing below this line can ever
        # request FEWER brokers than the cluster already has.
        if target_brokers <= current_brokers:
            logger.info(
                "refusing to scale %s: step %d would not increase the broker "
                "count (currently %d)",
                cluster_arn, step, current_brokers,
            )
            return {"scaled": False, "reason": "non-positive step", "current_brokers": current_brokers}

        if target_brokers > max_brokers:
            logger.info(
                "refusing to scale %s: %d brokers would exceed max_brokers %d "
                "(currently %d)",
                cluster_arn, target_brokers, max_brokers, current_brokers,
            )
            return {"scaled": False, "reason": "at max_brokers ceiling", "current_brokers": current_brokers}

        logger.info(
            "scaling %s from %d to %d brokers (step %d, ceiling %d)",
            cluster_arn, current_brokers, target_brokers, step, max_brokers,
        )
        response = kafka.update_broker_count(
            ClusterArn=cluster_arn,
            CurrentVersion=current_version,
            TargetNumberOfBrokerNodes=target_brokers,
        )
        logger.info(
            "update_broker_count accepted: %s",
            response.get("ClusterOperationArn"),
        )
        return {"scaled": True, "from": current_brokers, "to": target_brokers}
  PYTHON
}

resource "aws_lambda_function" "broker_scaler" {
  count = local.autoscaling_enabled ? 1 : 0

  function_name = "${var.name}-msk-broker-scaler"
  description   = "Adds brokers to ${var.name} when the scale-out alarm fires. Increase-only -- never scales in."

  role     = aws_iam_role.broker_scaler[0].arn
  filename = data.archive_file.broker_scaler[0].output_path
  // Recomputed whenever the inline source above changes, which is what
  // triggers a redeploy of the function's code.
  source_code_hash = data.archive_file.broker_scaler[0].output_base64sha256

  handler       = "handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  timeout       = 60

  environment {
    variables = {
      CLUSTER_ARN = aws_msk_cluster.this.arn
      MAX_BROKERS = tostring(var.autoscaling.max_brokers)
      STEP        = tostring(local.autoscaling_step)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.broker_scaler,
    aws_iam_role_policy.broker_scaler,
  ]
}

resource "aws_sns_topic_subscription" "broker_scaler" {
  count = local.autoscaling_enabled ? 1 : 0

  topic_arn = aws_sns_topic.broker_scaler[0].arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.broker_scaler[0].arn
}

resource "aws_lambda_permission" "broker_scaler_sns" {
  count = local.autoscaling_enabled ? 1 : 0

  statement_id  = "AllowExecutionFromSNS"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.broker_scaler[0].function_name
  // The SNS service principal. Fixed by AWS.
  principal  = "sns.amazonaws.com"
  source_arn = aws_sns_topic.broker_scaler[0].arn
}
