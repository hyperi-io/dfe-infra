// The contract, executable -- the msk/ body's half of it. Each body gets a
// contract-<body>.tftest.hcl beside this one, asserting the SAME contract
// against the same variables. A body that cannot satisfy an assertion has
// changed the contract rather than implemented it, and CONTRACT.md is what
// changes first.
//
// ONE FILE PER BODY, and not by preference: a file-level mock_provider has to
// resolve against the required_providers of EVERY run's module in that file, so
// a mock_provider "redpanda" alongside a run against ./msk resolves to a
// hashicorp/redpanda that does not exist. Declaring the source in this
// directory's own required_providers does not help -- a run block's module
// replaces the root, and that is what the local name is matched against.
//
// A module source cannot be a variable, which is why each run block names its
// body rather than the file being parameterised over them.
//
// Provider-free by construction: mock_provider means no credentials, no API
// call and no cost, so it runs in CI and on a laptop with no cloud account.

mock_provider "aws" {
  // A policy document's generated default is a random string, and the provider
  // rejects a policy that is not a JSON object.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  // The partition is half of every ARN this module builds, and the provider
  // validates the shape of an ARN before it ever calls AWS.
  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::000000000000:role/mock"
    }
  }

  mock_resource "aws_secretsmanager_secret" {
    defaults = {
      arn = "arn:aws:secretsmanager:us-west-2:000000000000:secret:AmazonMSK_mock-aBcDeF"
    }
  }

  // The provider validates an ARN's shape before it calls AWS, and a generated
  // default is a random string, so the cluster rejects the configuration it is
  // handed unless this one is supplied.
  mock_resource "aws_msk_configuration" {
    defaults = {
      arn = "arn:aws:kafka:us-west-2:000000000000:configuration/dfe-kafka-contract/00000000-0000-0000-0000-000000000000-3"
    }
  }

  // Both bootstrap strings and the UUID every topic and group ARN is built from
  // are computed, and supplying them here is what makes those outputs testable.
  mock_resource "aws_msk_cluster" {
    defaults = {
      arn                          = "arn:aws:kafka:us-west-2:000000000000:cluster/dfe-kafka-contract/00000000-0000-0000-0000-000000000000-3"
      cluster_uuid                 = "00000000-0000-0000-0000-000000000000-3"
      bootstrap_brokers_sasl_scram = "b-1.mock.kafka.us-west-2.amazonaws.com:9096,b-2.mock.kafka.us-west-2.amazonaws.com:9096"
      bootstrap_brokers_sasl_iam   = "b-1.mock.kafka.us-west-2.amazonaws.com:9098,b-2.mock.kafka.us-west-2.amazonaws.com:9098"
    }
  }

  // The autoscaler's SNS topic and Lambda ARNs feed the SNS subscription's
  // endpoint and the Lambda permission's source_arn, both of which the
  // provider checks are ARN-shaped before it would ever call AWS.
  mock_resource "aws_sns_topic" {
    defaults = {
      arn = "arn:aws:sns:us-west-2:000000000000:dfe-kafka-contract-msk-broker-scaler"
    }
  }

  mock_resource "aws_lambda_function" {
    defaults = {
      arn = "arn:aws:lambda:us-west-2:000000000000:function:dfe-kafka-contract-msk-broker-scaler"
    }
  }
}

variables {
  name = "dfe-kafka-contract"
  env  = "test"

  // 4.2.x.kraft is what `aws kafka list-kafka-versions --region us-west-2`
  // reports ACTIVE, read at build time rather than defaulted in the module.
  kafka_version = "4.2.x.kraft"

  network = {
    vpc_id             = "vpc-00000000000000000"
    cidr               = "10.90.0.0/16"
    azs                = ["mock-1a", "mock-1b", "mock-1c"]
    private_subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002", "subnet-00000000000000003"]
    public_subnet_ids  = ["subnet-00000000000000004", "subnet-00000000000000005", "subnet-00000000000000006"]
  }

  broker_shape_ref = "msk-broker"

  resolved_shapes = {
    msk-broker = {
      instance_types = ["express.m7g.large"]
      arch           = "arm64"
    }
  }

  broker_count = 3

  // No module default: the same three numbers a customer dial carries, so a
  // contract run proves what render_dial.py actually sends.
  num_partitions    = 12
  log_retention_ms  = 259200000
  message_max_bytes = 16777216

  kms_key_arn      = "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000"
  eks_cluster_name = "dfe-contract"

  scram_password = "contract-only-not-a-real-credential"

  pod_identity = {
    namespace       = "dfe"
    service_account = "kafka-bootstrap"
  }

  pod_identity_trust_policy_json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"

  // step left unset -- the contract's network carries 3 private subnets, so
  // the default step under test is that same 3.
  autoscaling = {
    max_brokers              = 6
    per_broker_capacity_mb_s = 50
    headroom                 = 1.3
  }
}

run "msk_contract_outputs" {
  command = plan

  module {
    source = "./msk"
  }

  assert {
    condition     = can(tostring(output.bootstrap))
    error_message = "bootstrap must be a string"
  }

  // Bare host:port pairs -- no scheme, no trailing comma, no list.
  assert {
    condition     = !strcontains(output.bootstrap, "://") && !endswith(output.bootstrap, ",")
    error_message = "bootstrap must be bare host:port pairs, comma separated"
  }

  assert {
    condition     = strcontains(output.bootstrap, ":9096")
    error_message = "bootstrap must be the SASL/SCRAM endpoint, which MSK serves on 9096"
  }

  assert {
    condition     = output.bootstrap_port == 9096
    error_message = "bootstrap_port must be the SASL/SCRAM port MSK serves on, a literal known at plan time"
  }

  // The literal above and the endpoint are two independent spellings, and the
  // aws root builds its forward target from both -- a drift between them aims
  // the tunnel at a closed port.
  assert {
    condition     = strcontains(output.bootstrap, ":${output.bootstrap_port}")
    error_message = "bootstrap_port must be the port bootstrap actually advertises"
  }

  assert {
    condition     = strcontains(output.bootstrap_iam, ":9098")
    error_message = "bootstrap_iam must be the SASL/IAM endpoint, which MSK serves on 9098"
  }

  assert {
    condition     = output.auth_type == "scram"
    error_message = "auth_type must be scram -- IAM is the bootstrap Job's path, not DFE's"
  }

  // A reference, never a value. An ARN is a name; a password is not.
  assert {
    condition     = startswith(output.credential_ref, "arn:")
    error_message = "credential_ref must be a secret ARN"
  }

  assert {
    condition     = length(output.network_attachment.subnet_ids) == 3
    error_message = "network_attachment.subnet_ids must carry the private subnet per zone the brokers live in"
  }

  assert {
    condition     = can(tostring(output.network_attachment.security_group_id))
    error_message = "network_attachment.security_group_id must be a string"
  }

  assert {
    condition     = startswith(output.cluster_arn, "arn:")
    error_message = "cluster_arn must be the MSK cluster ARN"
  }

  assert {
    condition     = startswith(output.bootstrap_role_arn, "arn:")
    error_message = "bootstrap_role_arn must be the role the in-cluster Job assumes"
  }
}

run "msk_express_shape_and_storage" {
  command = plan

  module {
    source = "./msk"
  }

  assert {
    condition     = aws_msk_cluster.this.broker_node_group_info[0].instance_type == "express.m7g.large"
    error_message = "the broker instance type must come from the resolved shape, never a literal"
  }

  // Express storage is MSK-managed, and the provider refuses a storage_info
  // block on an Express instance type.
  assert {
    condition     = length(aws_msk_cluster.this.broker_node_group_info[0].storage_info) == 0
    error_message = "no storage_info may be set on an Express cluster"
  }

  assert {
    condition     = length(aws_msk_cluster.this.broker_node_group_info[0].client_subnets) == 3
    error_message = "brokers take the private subnet in each zone"
  }
}

// The broker SG: egress narrowed to 443/tcp (no VPC interface endpoint covers
// Secrets Manager, KMS or CloudWatch Logs here), and the open_monitoring
// scrape ports opened from the VPC CIDR rather than client_cidrs.
run "msk_broker_security_group_is_scoped" {
  command = plan

  module {
    source = "./msk"
  }

  assert {
    condition     = aws_vpc_security_group_egress_rule.brokers.ip_protocol == "tcp"
    error_message = "broker egress must be tcp, not -1 (all protocols)"
  }

  assert {
    condition     = aws_vpc_security_group_egress_rule.brokers.from_port == 443 && aws_vpc_security_group_egress_rule.brokers.to_port == 443
    error_message = "broker egress must be scoped to 443, not every port"
  }

  assert {
    condition = length([
      for r in aws_vpc_security_group_ingress_rule.open_monitoring : r
      if r.from_port == 11001 && r.cidr_ipv4 == var.network.cidr
    ]) == 1
    error_message = "the JMX exporter port (11001) must be open from the VPC CIDR for the otel-collector scrape"
  }

  assert {
    condition = length([
      for r in aws_vpc_security_group_ingress_rule.open_monitoring : r
      if r.from_port == 11002 && r.cidr_ipv4 == var.network.cidr
    ]) == 1
    error_message = "the node exporter port (11002) must be open from the VPC CIDR for the otel-collector scrape"
  }
}

// B3: IAM for the bootstrap Job, SCRAM for DFE. Both on one cluster is what
// lets the Job create the first ACL.
run "msk_both_sasl_mechanisms" {
  command = plan

  module {
    source = "./msk"
  }

  assert {
    condition     = aws_msk_cluster.this.client_authentication[0].sasl[0].scram == true
    error_message = "SASL/SCRAM must be on -- it is what DFE authenticates with"
  }

  assert {
    condition     = aws_msk_cluster.this.client_authentication[0].sasl[0].iam == true
    error_message = "SASL/IAM must be on -- it is the bootstrap Job's only way in before any ACL exists"
  }

  assert {
    condition     = aws_msk_cluster.this.encryption_info[0].encryption_in_transit[0].client_broker == "TLS"
    error_message = "client to broker traffic must be TLS"
  }

  assert {
    condition     = aws_msk_cluster.this.rebalancing[0].status == "ACTIVE"
    error_message = "intelligent rebalancing must be declared ACTIVE, not left to the API default"
  }
}

// The four settings Express accepts, plus the size chain it permits in full.
run "msk_configuration_contents" {
  command = plan

  module {
    source = "./msk"
  }

  assert {
    condition     = strcontains(aws_msk_configuration.this.server_properties, "auto.create.topics.enable=false")
    error_message = "auto.create.topics.enable must render false -- the bootstrap Job pre-creates the landing topics so their partitions and retention are ours to set"
  }

  assert {
    condition     = strcontains(aws_msk_configuration.this.server_properties, "message.max.bytes=16777216")
    error_message = "message.max.bytes must carry the 16 MiB the whole size chain uses"
  }

  assert {
    condition     = strcontains(aws_msk_configuration.this.server_properties, "replica.fetch.max.bytes=16777216")
    error_message = "replica.fetch.max.bytes must match message.max.bytes or replication stalls on a large record"
  }

  assert {
    condition     = strcontains(aws_msk_configuration.this.server_properties, "num.partitions=12")
    error_message = "num.partitions must come from the variable the chart's derivation feeds"
  }
}

// MSK refuses a SCRAM secret whose name does not start with AmazonMSK_, and
// refuses one on the default aws/secretsmanager key.
run "msk_scram_secret_shape" {
  command = plan

  module {
    source = "./msk"
  }

  assert {
    condition     = startswith(aws_secretsmanager_secret.scram.name, "AmazonMSK_")
    error_message = "the SCRAM secret's name must start with AmazonMSK_"
  }

  assert {
    condition     = aws_secretsmanager_secret.scram.kms_key_id == var.kms_key_arn
    error_message = "the SCRAM secret must be encrypted with the deployment's customer-managed key"
  }
}

// The API requires a multiple of the client subnet count, and an uneven cluster
// loads one zone harder than the others.
run "msk_rejects_uneven_broker_count" {
  command = plan

  module {
    source = "./msk"
  }

  variables {
    broker_count = 4
  }

  expect_failures = [
    var.broker_count,
  ]
}

// A Standard broker type cannot be converted to Express afterwards, so it has
// to fail here rather than build the cluster we would have to rebuild.
run "msk_rejects_standard_broker_type" {
  command = plan

  module {
    source = "./msk"
  }

  variables {
    resolved_shapes = {
      msk-broker = {
        instance_types = ["kafka.m7g.large"]
        arch           = "arm64"
      }
    }
  }

  expect_failures = [
    var.resolved_shapes,
  ]
}

// One CloudWatch metric per broker, and a metric math expression takes ten. The
// refusal belongs at plan: at apply the cluster, the Lambda, the SNS topic and
// the IAM all exist by the time PutMetricAlarm rejects the alarm.
run "msk_autoscaling_refuses_more_brokers_than_one_alarm_can_name" {
  command = plan

  module {
    source = "./msk"
  }

  variables {
    broker_count = 12
    autoscaling = {
      enabled                  = true
      max_brokers              = 12
      per_broker_capacity_mb_s = 50
      headroom                 = 1.3
    }
  }

  expect_failures = [
    var.autoscaling,
  ]
}

// Scaling by hand is the way past that ceiling, so the ceiling must not refuse a
// cluster that never asked for an alarm.
run "msk_a_large_cluster_plans_cleanly_with_autoscaling_off" {
  command = plan

  module {
    source = "./msk"
  }

  variables {
    broker_count = 12
    autoscaling = {
      enabled                  = false
      max_brokers              = 12
      per_broker_capacity_mb_s = 50
      headroom                 = 1.3
    }
  }

  assert {
    condition     = length(aws_cloudwatch_metric_alarm.broker_scale_out) == 0
    error_message = "autoscaling off must render no alarm, whatever the broker count"
  }
}

// S1: the alarm threshold arithmetic -- broker_count x per_broker_capacity_mb_s
// x 1e6 x headroom, with none of the four factors a literal in the resource.
run "msk_autoscaler_threshold_arithmetic" {
  command = plan

  module {
    source = "./msk"
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.broker_scale_out[0].threshold == 3 * 50 * 1000000 * 1.3
    error_message = "the alarm threshold must be broker_count x per_broker_capacity_mb_s x 1e6 x headroom"
  }

  // The threshold above is bytes PER SECOND, so the metric compared against
  // it has to be a rate too. BytesInPerSec is published once a minute, so a
  // 300s period holds 5 samples -- the statistic has to be Average
  // (Sum/SampleCount), not Sum (the raw total of those 5 already-averaged
  // rate samples), or the compared value runs ~5x hot and the alarm fires at
  // a fraction of the traffic it was sized for.
  assert {
    // metric_query is a SET of objects (no addressable index), so the
    // per-broker queries are picked out by the id prefix instead.
    condition = alltrue([
      for mq in aws_cloudwatch_metric_alarm.broker_scale_out[0].metric_query :
      mq.metric[0].stat == "Average" if startswith(mq.id, "broker_")
    ])
    error_message = "the per-broker statistic must be Average, not Sum, or the cluster-wide figure is not actually bytes per second"
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.broker_scale_out[0].evaluation_periods == 3 && aws_cloudwatch_metric_alarm.broker_scale_out[0].datapoints_to_alarm == 3
    error_message = "the alarm must require 3 of 3 datapoints -- that is the whole of its stabilisation, since nothing here re-arms"
  }

  // CloudWatch rejects SEARCH on a metric alarm with ValidationError: SEARCH
  // is not supported on Metric Alarms, and a mocked provider never runs that
  // validation, so the assertion is what keeps the expression out.
  assert {
    condition = alltrue([
      for mq in aws_cloudwatch_metric_alarm.broker_scale_out[0].metric_query :
      mq.expression == null ? true : !strcontains(mq.expression, "SEARCH")
    ])
    error_message = "no metric_query may use SEARCH -- CloudWatch refuses PutMetricAlarm outright, whatever else the alarm carries"
  }

  // One query per broker, so the sum below covers the whole dialled cluster.
  assert {
    condition = length([
      for mq in aws_cloudwatch_metric_alarm.broker_scale_out[0].metric_query : mq if startswith(mq.id, "broker_")
    ]) == 3
    error_message = "the alarm must carry one metric_query per broker id, and this contract dials broker_count = 3"
  }

  // Every per-broker query names one broker of one cluster, so a second MSK
  // cluster's traffic can never land in this alarm's sum.
  assert {
    condition = alltrue([
      for broker_id in [1, 2, 3] :
      length([
        for mq in aws_cloudwatch_metric_alarm.broker_scale_out[0].metric_query :
        mq
        if mq.id == "broker_${broker_id}"
        && mq.metric[0].metric_name == "BytesInPerSec"
        && mq.metric[0].namespace == "AWS/Kafka"
        && mq.metric[0].dimensions["Broker ID"] == tostring(broker_id)
        && mq.metric[0].dimensions["Cluster Name"] == aws_msk_cluster.this.cluster_name
      ]) == 1
    ])
    error_message = "each broker id 1..broker_count needs its own AWS/Kafka BytesInPerSec query dimensioned by Cluster Name and Broker ID"
  }

  // Each per-broker query carries the granularity, which is where the alarm's
  // period lives once the expression no longer embeds one.
  assert {
    condition = alltrue([
      for mq in aws_cloudwatch_metric_alarm.broker_scale_out[0].metric_query :
      mq.metric[0].period == 300 if startswith(mq.id, "broker_")
    ])
    error_message = "every per-broker metric_query must carry period = 300, the window the threshold is sized against"
  }

  // Exactly one query returns data, and it is the sum the threshold compares.
  assert {
    condition = length([
      for mq in aws_cloudwatch_metric_alarm.broker_scale_out[0].metric_query : mq if mq.return_data
    ]) == 1
    error_message = "exactly one metric_query may set return_data -- CloudWatch evaluates the alarm against that one series"
  }

  assert {
    condition = (
      [for mq in aws_cloudwatch_metric_alarm.broker_scale_out[0].metric_query : mq if mq.id == "cluster_bytes_in"][0].expression
      == "SUM([broker_1,broker_2,broker_3])"
    )
    error_message = "the returned query must sum every per-broker series, or the threshold is compared against part of the cluster"
  }

  assert {
    condition     = [for mq in aws_cloudwatch_metric_alarm.broker_scale_out[0].metric_query : mq if mq.id == "cluster_bytes_in"][0].return_data
    error_message = "the summing expression is the series the alarm watches, so it is the one that returns data"
  }

  assert {
    // alarm_actions is a set, so its members have no index to select by.
    condition     = contains(aws_cloudwatch_metric_alarm.broker_scale_out[0].alarm_actions, aws_sns_topic.broker_scaler[0].arn)
    error_message = "the alarm must notify the broker-scaler SNS topic on ALARM"
  }
}

// S2: the Lambda's env carries only what an increase can act on -- no
// min_brokers, no scale-in knob of any kind.
run "msk_autoscaler_lambda_is_increase_only" {
  command = plan

  module {
    source = "./msk"
  }

  assert {
    condition     = aws_lambda_function.broker_scaler[0].environment[0].variables["MAX_BROKERS"] == "6"
    error_message = "MAX_BROKERS must come from var.autoscaling.max_brokers"
  }

  assert {
    condition     = aws_lambda_function.broker_scaler[0].environment[0].variables["STEP"] == "3"
    error_message = "STEP must default to the client subnet count (3 in this contract's network) when autoscaling.step is unset"
  }

  assert {
    condition     = length(aws_lambda_function.broker_scaler[0].environment[0].variables) == 3
    error_message = "the Lambda's env must carry exactly CLUSTER_ARN, MAX_BROKERS and STEP -- an extra key here would be a scale-down or re-arm knob this design does not have"
  }

  assert {
    condition     = aws_lambda_function.broker_scaler[0].runtime == "python3.13" && aws_lambda_function.broker_scaler[0].architectures[0] == "arm64"
    error_message = "the scaler Lambda must run python3.13 on arm64"
  }
}

// otel is the default: DFE's monitoring goes to its own OTel feed and
// HyperDX, never CloudWatch, so broker logs land in a bucket this body creates
// instead of a log group.
run "msk_telemetry_otel_ships_broker_logs_to_s3" {
  command = plan

  module {
    source = "./msk"
  }

  assert {
    condition     = length(aws_msk_cluster.this.logging_info[0].broker_logs[0].s3) == 1
    error_message = "otel (the default) must deliver broker logs to S3"
  }

  assert {
    condition     = length(aws_msk_cluster.this.logging_info[0].broker_logs[0].cloudwatch_logs) == 0
    error_message = "otel must render no CloudWatch broker log delivery"
  }

  assert {
    condition     = length(aws_cloudwatch_log_group.brokers) == 0
    error_message = "otel must create no CloudWatch log group for broker logs"
  }

  assert {
    condition     = length(aws_s3_bucket.broker_logs) == 1
    error_message = "otel must create exactly one broker-log bucket"
  }

  assert {
    condition     = aws_s3_bucket.broker_logs[0].force_destroy == true
    error_message = "the broker-log bucket is log spill, not data DFE keeps -- force_destroy must be true"
  }

  assert {
    condition     = aws_s3_bucket_public_access_block.broker_logs[0].block_public_acls == true && aws_s3_bucket_public_access_block.broker_logs[0].block_public_policy == true && aws_s3_bucket_public_access_block.broker_logs[0].ignore_public_acls == true && aws_s3_bucket_public_access_block.broker_logs[0].restrict_public_buckets == true
    error_message = "the broker-log bucket must block public access on all four settings"
  }

  assert {
    // rule and apply_server_side_encryption_by_default are both sets (order
    // is not addressable), and one() reads the single element each carries.
    condition     = one(one(aws_s3_bucket_server_side_encryption_configuration.broker_logs[0].rule).apply_server_side_encryption_by_default).kms_master_key_id == var.kms_key_arn
    error_message = "the broker-log bucket must be encrypted with the deployment's own key"
  }

  assert {
    // rule is a list here (unlike the SSE config's rule set above), so a
    // direct index reaches .expiration without pulling the whole rule
    // object through one() -- which would otherwise carry the AWS
    // provider's deprecated rule.prefix attribute along for the ride and
    // trip OpenTofu's "value derived from a deprecated source" warning
    // even though this module never sets prefix.
    condition     = one(aws_s3_bucket_lifecycle_configuration.broker_logs[0].rule[0].expiration).days == 2
    error_message = "the lifecycle expiry must match telemetry.retention_days -- 2 is the otel default"
  }

  assert {
    condition     = output.broker_log_bucket == aws_s3_bucket.broker_logs[0].bucket
    error_message = "broker_log_bucket must be the bucket the fetcher's object-store source reads"
  }
}

// cloudwatch is the opt-in AWS-native path: short-retention, no S3 bucket.
run "msk_telemetry_cloudwatch_keeps_the_aws_native_path" {
  command = plan

  module {
    source = "./msk"
  }

  variables {
    telemetry = {
      sink           = "cloudwatch"
      retention_days = 7
    }
  }

  assert {
    condition     = length(aws_msk_cluster.this.logging_info[0].broker_logs[0].cloudwatch_logs) == 1
    error_message = "cloudwatch must deliver broker logs to a CloudWatch log group"
  }

  assert {
    condition     = length(aws_msk_cluster.this.logging_info[0].broker_logs[0].s3) == 0
    error_message = "cloudwatch must render no S3 broker log delivery"
  }

  assert {
    condition     = length(aws_s3_bucket.broker_logs) == 0
    error_message = "cloudwatch must create no broker-log bucket"
  }

  assert {
    condition     = aws_cloudwatch_log_group.brokers[0].retention_in_days == 7
    error_message = "the CloudWatch log group's retention must come from telemetry.retention_days"
  }

  assert {
    condition     = output.broker_log_bucket == ""
    error_message = "broker_log_bucket must be empty when the sink is cloudwatch"
  }
}

// S3: enabled = false renders none of it -- no alarm, no topic, no Lambda.
run "msk_autoscaler_disabled_renders_nothing" {
  command = plan

  module {
    source = "./msk"
  }

  variables {
    autoscaling = {
      enabled                  = false
      max_brokers              = 6
      per_broker_capacity_mb_s = 50
      headroom                 = 1.3
    }
  }

  assert {
    condition     = length(aws_cloudwatch_metric_alarm.broker_scale_out) == 0
    error_message = "enabled = false must render no alarm"
  }

  assert {
    condition     = length(aws_sns_topic.broker_scaler) == 0
    error_message = "enabled = false must render no SNS topic"
  }

  assert {
    condition     = length(aws_lambda_function.broker_scaler) == 0
    error_message = "enabled = false must render no Lambda"
  }

  assert {
    condition     = length(aws_iam_role.broker_scaler) == 0
    error_message = "enabled = false must render no scaler IAM role"
  }
}

// An account guardrail that refuses CreateRole without a boundary refuses the
// first role this body creates without one, so EVERY role carries it.
run "msk_every_role_carries_the_permissions_boundary" {
  command = plan

  module {
    source = "./msk"
  }

  variables {
    permissions_boundary = "arn:aws:iam::000000000000:policy/contract-boundary"
    iam_path             = "/dfe-e2e/"
    s3_bucket_prefix     = "dfe-e2e-"
  }

  assert {
    condition     = length(aws_iam_role.broker_scaler) == 1 && length(aws_s3_bucket.broker_logs) == 1
    error_message = "this case must build the scaler role and the broker-log bucket, or the assertions below prove nothing about them"
  }

  assert {
    condition = alltrue(concat(
      [aws_iam_role.bootstrap.path == "/dfe-e2e/"],
      [for role in aws_iam_role.broker_scaler : role.path == "/dfe-e2e/"],
    ))
    error_message = "every aws_iam_role in the msk body must sit under var.iam_path"
  }

  assert {
    condition     = alltrue([for bucket in aws_s3_bucket.broker_logs : startswith(bucket.bucket, "dfe-e2e-")])
    error_message = "the broker-log bucket must carry var.s3_bucket_prefix"
  }

  assert {
    condition = alltrue(concat(
      [aws_iam_role.bootstrap.permissions_boundary == "arn:aws:iam::000000000000:policy/contract-boundary"],
      [for role in aws_iam_role.broker_scaler : role.permissions_boundary == "arn:aws:iam::000000000000:policy/contract-boundary"],
    ))
    error_message = "every aws_iam_role in the msk body must carry var.permissions_boundary"
  }
}
