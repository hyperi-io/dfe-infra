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

  // The schedule's and the Lambda's ARNs feed the event target and the Lambda
  // permission's source_arn, which the provider checks are ARN-shaped before it
  // would ever call AWS.
  mock_resource "aws_cloudwatch_event_rule" {
    defaults = {
      arn = "arn:aws:events:us-west-2:000000000000:rule/dfe-kafka-contract-msk-broker-scaler"
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

  // What the aws root sends: the dial's telemetry block, and the persistent window for a non-ephemeral deployment.
  telemetry                   = { sink = "otel", retention_days = 2, forced_retention_days = 1 }
  secret_recovery_window_days = 30

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
    period_seconds           = 300
    evaluation_periods       = 3
  }

  // What the aws root sends from versions.yaml's lambda-runtimes.
  broker_scaler_runtime = "python3.14"
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
    error_message = "auto.create.topics.enable must render false -- dfe-engine creates the landing topics so their partitions and retention are ours to set"
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

// The scaler reads every broker through GetMetricData, which takes 500 queries,
// so a cluster past the ten metrics one alarm could name still autoscales.
run "msk_a_large_cluster_still_autoscales" {
  command = plan

  module {
    source = "./msk"
  }

  variables {
    broker_count = 12
    autoscaling = {
      enabled                  = true
      max_brokers              = 15
      per_broker_capacity_mb_s = 50
      headroom                 = 1.3
      period_seconds           = 300
      evaluation_periods       = 3
    }
  }

  assert {
    condition     = length(aws_lambda_function.broker_scaler) == 1 && length(aws_cloudwatch_event_rule.broker_scaler) == 1
    error_message = "a 12-broker cluster with autoscaling on builds its scaler and schedule"
  }
}

// Autoscaling off builds no scaler at all, whatever the broker count.
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
      period_seconds           = 300
      evaluation_periods       = 3
    }
  }

  assert {
    condition     = length(aws_cloudwatch_event_rule.broker_scaler) == 0
    error_message = "autoscaling off must render no schedule, whatever the broker count"
  }
}

// S1: the per-broker threshold -- per_broker_capacity_mb_s x 1e6 x headroom,
// the same at every broker count, with none of the factors a literal.
run "msk_autoscaler_threshold_and_schedule" {
  command = plan

  module {
    source = "./msk"
  }

  assert {
    condition     = tonumber(aws_lambda_function.broker_scaler[0].environment[0].variables["THRESHOLD_BYTES_PER_SEC_PER_BROKER"]) == 50 * 1000000 * 1.3
    error_message = "the per-broker threshold must be per_broker_capacity_mb_s x 1e6 x headroom"
  }

  assert {
    condition = (
      aws_lambda_function.broker_scaler[0].environment[0].variables["PERIOD_SECONDS"] == "300"
      && aws_lambda_function.broker_scaler[0].environment[0].variables["EVALUATION_PERIODS"] == "3"
    )
    error_message = "the scaler judges autoscaling.evaluation_periods periods of autoscaling.period_seconds"
  }

  // Metrics are dimensioned by cluster name, so the scaler must be told the one MSK built.
  assert {
    condition     = aws_lambda_function.broker_scaler[0].environment[0].variables["CLUSTER_NAME"] == aws_msk_cluster.this.cluster_name
    error_message = "CLUSTER_NAME is the cluster's own name"
  }

  assert {
    condition     = aws_cloudwatch_event_rule.broker_scaler[0].schedule_expression == "rate(5 minutes)"
    error_message = "the schedule runs every autoscaling.period_seconds"
  }

  assert {
    condition     = aws_cloudwatch_event_target.broker_scaler[0].arn == aws_lambda_function.broker_scaler[0].arn && aws_lambda_permission.broker_scaler_schedule[0].source_arn == aws_cloudwatch_event_rule.broker_scaler[0].arn
    error_message = "the schedule invokes the scaler, and only that schedule may"
  }

  assert {
    condition     = aws_lambda_function.broker_scaler[0].handler == "broker_scaler.handler"
    error_message = "the Lambda runs broker_scaler.py's handler, the file scripts/tests/test_msk_broker_scaler.py exercises"
  }
}

// The window is the caller's: a value other than the dial's default reaches the scaler unchanged.
run "msk_autoscaler_takes_the_callers_window" {
  command = plan

  module {
    source = "./msk"
  }

  variables {
    autoscaling = {
      max_brokers              = 6
      per_broker_capacity_mb_s = 50
      headroom                 = 1.3
      period_seconds           = 60
      evaluation_periods       = 5
    }
  }

  assert {
    condition     = aws_cloudwatch_event_rule.broker_scaler[0].schedule_expression == "rate(1 minute)"
    error_message = "a 60-second period is EventBridge's singular rate(1 minute)"
  }

  assert {
    condition     = aws_lambda_function.broker_scaler[0].environment[0].variables["EVALUATION_PERIODS"] == "5"
    error_message = "autoscaling.evaluation_periods reaches the scaler"
  }
}

run "msk_autoscaler_refuses_a_period_that_is_not_whole_minutes" {
  command = plan

  module {
    source = "./msk"
  }

  variables {
    autoscaling = {
      max_brokers              = 6
      per_broker_capacity_mb_s = 50
      headroom                 = 1.3
      period_seconds           = 90
      evaluation_periods       = 3
    }
  }

  expect_failures = [var.autoscaling]
}

// S2: the Lambda's env carries only what an increase can act on -- no
// min_brokers, no scale-in dial of any kind.
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
    condition = toset(keys(aws_lambda_function.broker_scaler[0].environment[0].variables)) == toset([
      "CLUSTER_ARN", "CLUSTER_NAME", "MAX_BROKERS", "STEP",
      "THRESHOLD_BYTES_PER_SEC_PER_BROKER", "PERIOD_SECONDS", "EVALUATION_PERIODS",
    ])
    error_message = "the Lambda's env carries what an increase acts on and no more -- an extra key would be a scale-down dial this design does not have"
  }

  assert {
    condition     = aws_lambda_function.broker_scaler[0].runtime == var.broker_scaler_runtime && aws_lambda_function.broker_scaler[0].architectures[0] == "arm64"
    error_message = "the scaler Lambda must run var.broker_scaler_runtime, the pin versions.yaml's lambda-runtimes renders, on arm64"
  }

  assert {
    condition     = aws_cloudwatch_log_group.broker_scaler[0].retention_in_days == var.telemetry.forced_retention_days
    error_message = "the scaler's Lambda log group must keep telemetry.forced_retention_days, never the Lambda default of never expire"
  }
}

// The runtime arrives in tfvars and the module guesses none: an empty pin, which
// the root sends when lambda_runtimes lacks the function, is refused at plan.
run "msk_scaler_runtime_must_be_a_python_runtime" {
  command = plan

  module {
    source = "./msk"
  }

  variables {
    broker_scaler_runtime = ""
  }

  expect_failures = [var.broker_scaler_runtime]
}

run "msk_scaler_runtime_is_checked_with_autoscaling_off" {
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
      period_seconds           = 300
      evaluation_periods       = 3
    }
    broker_scaler_runtime = ""
  }

  expect_failures = [var.broker_scaler_runtime]
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
    condition     = one(aws_s3_bucket_lifecycle_configuration.broker_logs[0].rule[0].expiration).days == var.telemetry.retention_days
    error_message = "the lifecycle expiry must match telemetry.retention_days"
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
      sink                  = "cloudwatch"
      retention_days        = 7
      forced_retention_days = 1
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

// S3: enabled = false renders none of it -- no schedule, no Lambda, no role.
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
      period_seconds           = 300
      evaluation_periods       = 3
    }
  }

  assert {
    condition     = length(aws_cloudwatch_event_rule.broker_scaler) == 0
    error_message = "enabled = false must render no schedule"
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

run "msk_bootstrap_role_holds_only_the_acl_write_grants" {
  command = plan

  module {
    source = "./msk"
  }

  assert {
    condition     = length(data.aws_iam_policy_document.bootstrap.statement) == 1
    error_message = "the bootstrap role grants at cluster scope only: the Job writes ACLs and creates no topic"
  }

  assert {
    condition = toset(data.aws_iam_policy_document.bootstrap.statement[0].actions) == toset([
      "kafka-cluster:Connect",
      "kafka-cluster:DescribeCluster",
      "kafka-cluster:AlterCluster",
    ])
    error_message = "an ACL write needs Connect, DescribeCluster and AlterCluster, and nothing more"
  }
}
