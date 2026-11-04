// The aws root's external ClickHouse wiring, executable: the facts it hands
// bootstrap for each mode, the shapes it refuses before anything is planned, and
// the cluster's egress addresses and own ranges.
//
// Provider-free by construction, like kafka.tftest.hcl beside it: every provider
// is mocked and every module this file does not exercise is overridden.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "000000000000"
    }
  }

  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }

  mock_data "aws_region" {
    defaults = {
      region = "us-west-2"
    }
  }

  // CloudTrail's CloudWatch role is built under both sinks and takes its policies from documents.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  // The Pod Identity association and CloudTrail refuse an ARN that is not one.
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::000000000000:role/mock"
    }
  }

  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      arn = "arn:aws:logs:us-west-2:000000000000:log-group:/aws/mock"
    }
  }
}

mock_provider "random" {}

mock_provider "confluent" {}

mock_provider "redpanda" {}

override_module {
  target = module.cluster

  outputs = {
    cluster_name              = "dfe-example"
    cluster_endpoint          = "https://00000000000000000000000000000000.gr7.us-west-2.eks.amazonaws.com"
    cluster_created_at        = "2026-01-01T00:00:00Z"
    cluster_security_group_id = "sg-00000000000000000"
    private_zone_arn          = "arn:aws:route53:::hostedzone/Z0000000000000000000"
    kms_key_arn               = "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000"
    nat_public_ips            = ["198.51.100.1", "198.51.100.2"]
    service_cidr              = "172.20.0.0/16"

    pod_identity_trust_policy_json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"

    audit_log_group                = "/aws/eks/dfe-example/cluster"
    clickhouse_object_store_bucket = "example-bucket"

    // What the module reports on a persistent deployment; the root ignores it on an ephemeral one.
    retained_on_destroy = {
      "aws_kms_key.retained[0]"                                  = "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000"
      "module.clickhouse_object_store.aws_s3_bucket.retained[0]" = "arn:aws:s3:::example-bucket"
    }
    clickhouse_object_store_endpoint = "https://example-bucket.s3.us-west-2.amazonaws.com/dfe/"

    network = {
      vpc_id             = "vpc-00000000000000000"
      cidr               = "10.90.0.0/16"
      azs                = ["us-west-2a", "us-west-2b", "us-west-2c"]
      private_subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002", "subnet-00000000000000003"]
      public_subnet_ids  = ["subnet-00000000000000004", "subnet-00000000000000005", "subnet-00000000000000006"]
    }

    karpenter = {
      interruption_queue  = "dfe-example"
      instance_profile    = "dfe-example-karpenter-node"
      discovery_tag       = "dfe-example"
      controller_role_arn = "arn:aws:iam::000000000000:role/dfe-example-karpenter"
      node_role_arn       = "arn:aws:iam::000000000000:role/dfe-example-karpenter-node"
    }
  }
}

override_data {
  target = module.secrets.data.aws_iam_policy_document.eso

  values = {
    json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
  }
}

override_resource {
  target = module.secrets.aws_iam_role.eso

  values = {
    arn = "arn:aws:iam::000000000000:role/dfe-example-external-secrets"
  }
}

override_data {
  target = module.secrets.data.aws_iam_policy_document.engine

  values = {
    json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
  }
}

override_resource {
  target = module.secrets.aws_iam_role.engine

  values = {
    arn = "arn:aws:iam::000000000000:role/dfe-example-engine-secrets"
  }
}

override_data {
  target = module.secrets.data.aws_iam_policy_document.receiver

  values = {
    json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
  }
}

override_resource {
  target = module.secrets.aws_iam_role.receiver

  values = {
    arn = "arn:aws:iam::000000000000:role/dfe-example-receiver-secrets"
  }
}

override_module {
  target = module.toolbox

  outputs = {
    instance_id          = ""
    ssm_session_document = ""
    targets              = {}
    admin_cidr           = ""
    security_group_id    = ""
    iam_role_name        = ""
  }
}

// RFC 5737 addresses and the reserved example domain only: this repository is
// public.
variables {
  provision = {
    cloud   = "aws"
    account = "000000000000"
    region  = "us-west-2"
    cidr    = "10.90.0.0/16"
  }

  name               = "dfe-example"
  env                = "test"
  profile            = "single"
  kubernetes_version = "1.36"

  // What scripts/resolve_sizing.py images writes into node-images.auto.tfvars.json.
  node_image = { family = "AL2023_ARM_64_STANDARD", version = "1.36.4-20261012" }

  node_pools = {
    system = {
      shape_ref     = "eks-system"
      min_size      = 2
      max_size      = 3
      desired_size  = 2
      capacity_type = "ON_DEMAND"
      disk_gb       = 40
      labels        = {}
      taints        = []
    }
  }

  resolved_shapes = {
    eks-system = {
      instance_types = ["m7g.large"]
      arch           = "arm64"
      root_volume    = { type = "gp3", size_gib = 40 }
    }
    toolbox = {
      instance_types = ["t4g.small"]
      arch           = "arm64"
      root_volume    = { type = "gp3", size_gib = 20 }
    }
  }

  eks_addon_versions = {
    "aws-ebs-csi-driver"     = "v1.66.0-eksbuild.1"
    "coredns"                = "v1.14.7-eksbuild.11"
    "eks-pod-identity-agent" = "v1.4.0-eksbuild.3"
    "kube-proxy"             = "v1.36.0-eksbuild.45"
    "vpc-cni"                = "v1.23.2-eksbuild.1"
  }

  lambda_runtimes = {
    "msk-broker-scaler" = "python3.14"
  }

  network   = { nat = "single", az_count = 3, subnet_newbits = 4 }
  endpoint  = { public = false, allowed_cidrs = [] }
  dns       = { private_zone = "dfe-example.internal", public_zone = "" }
  telemetry = { sink = "otel", retention_days = 2, forced_retention_days = 1, eks_log_types = ["audit"] }

  storage_class = "gp3"

  edge       = { enabled = false, tunnel = { client_cidr = "100.64.0.0/10" } }
  cloudtrail = { enabled = false }

  kafka = { provider = "strimzi" }

  toolbox = {
    enabled                    = false
    aws                        = { shape_ref = "toolbox" }
    ttl_minutes                = 60
    session                    = { idle_timeout_minutes = 15, max_duration_minutes = 240 }
    session_log_retention_days = 90
  }

  secrets = { backend = "aws-sm", ref = "" }

  seeds = {
    "kafka/strimzi" = { password = "" }
    "ui/nextauth"   = { secret = "" }
  }

  endpoints = {
    clickhouse_host = "dfe-clickhouse.clickhouse.svc.cluster.local"
    kafka_bootstrap = "dfe-kafka.strimzi.svc.cluster.local:9092"
    otel_endpoint   = "otel-collector-gateway.otel.svc.cluster.local:4317"
  }

  repo_url        = "https://github.com/example/dfe-deploy.git"
  target_revision = "main"

  state = {
    bucket = "example-tfstate"
    key    = "dfe/test/aws.tfstate"
    region = "us-west-2"
  }

  tags = {
    service-name      = "dfe"
    service-namespace = "example"
    environment       = "test"
    owner             = "owner@example.com"
    cost-center       = "experiments"
    lifecycle         = "ephemeral"
    iac-source        = "dfe-infra/terraform/environments/aws"
  }
}

// No clickhouse block is the in-cluster server: every external fact is empty,
// so bootstrap writes nothing that moves a chart.
run "no_block_keeps_clickhouse_in_the_cluster" {
  command = plan

  assert {
    condition     = output.DFE_CLICKHOUSE_MODE == "in_cluster"
    error_message = "an unstated mode is the in-cluster server"
  }

  assert {
    condition = (
      output.DFE_CLICKHOUSE_PORT == "" && output.DFE_CLICKHOUSE_NATIVE_PORT == ""
      && output.DFE_CLICKHOUSE_SECURE == "" && output.DFE_CLICKHOUSE_USER == ""
    )
    error_message = "an in-cluster server hands bootstrap no external port, scheme or user"
  }

  assert {
    condition     = output.DFE_CLICKHOUSE_HOST == "dfe-clickhouse.clickhouse.svc.cluster.local"
    error_message = "the host stays whatever endpoints.clickhouse_host says"
  }
}

// ClickHouse Cloud: the host on the endpoint the root already reads, the TLS
// ports and the password from its own sensitive variable.
run "external_hands_bootstrap_every_fact" {
  command = plan

  variables {
    clickhouse = { mode = "external" }

    endpoints = {
      clickhouse_host = "ch.example.com"
      kafka_bootstrap = "dfe-kafka.strimzi.svc.cluster.local:9092"
      otel_endpoint   = "otel-collector-gateway.otel.svc.cluster.local:4317"
    }

    seeds = {
      "kafka/strimzi"       = { password = "" }
      "ui/nextauth"         = { secret = "" }
      "clickhouse/external" = { password = "" }
    }

    clickhouse_external_password = "contract-only-not-a-real-credential"
  }

  assert {
    condition     = output.DFE_CLICKHOUSE_MODE == "external" && output.DFE_CLICKHOUSE_HOST == "ch.example.com"
    error_message = "external mode hands bootstrap the mode and the external host"
  }

  assert {
    condition     = output.DFE_CLICKHOUSE_PORT == "8443" && output.DFE_CLICKHOUSE_NATIVE_PORT == "9440"
    error_message = "the HTTPS and native TLS ports default to ClickHouse Cloud's, as strings"
  }

  assert {
    condition     = output.DFE_CLICKHOUSE_SECURE == "true" && output.DFE_CLICKHOUSE_USER == "default"
    error_message = "secure defaults to the literal true a shell compares, and the user to default"
  }
}

// A ClickHouse Cloud service on aws is the offered one. Its host names the
// cloud, so the root hands it to managed-backends, whose refusal of any other
// cloud is proven in that module's own tests.
run "an_aws_clickhouse_cloud_service_is_the_offered_one" {
  command = plan

  variables {
    clickhouse = { mode = "external" }

    endpoints = {
      clickhouse_host = "abc123.us-west-2.aws.clickhouse.cloud"
      kafka_bootstrap = "dfe-kafka.strimzi.svc.cluster.local:9092"
      otel_endpoint   = "otel-collector-gateway.otel.svc.cluster.local:4317"
    }

    seeds = {
      "kafka/strimzi"       = { password = "" }
      "ui/nextauth"         = { secret = "" }
      "clickhouse/external" = { password = "" }
    }

    clickhouse_external_password = "contract-only-not-a-real-credential"
  }

  assert {
    condition     = module.managed_backends.clickhouse.service == "clickhouse-cloud" && module.managed_backends.clickhouse.vendor_cloud == "aws"
    error_message = "the root must hand managed-backends the external host and its mode"
  }
}

// A server the deployer runs is not a vendor's service, so no cloud is read off it.
run "a_byo_server_is_not_a_clickhouse_cloud_service" {
  command = plan

  variables {
    clickhouse = { mode = "external" }

    endpoints = {
      clickhouse_host = "ch.example.com"
      kafka_bootstrap = "dfe-kafka.strimzi.svc.cluster.local:9092"
      otel_endpoint   = "otel-collector-gateway.otel.svc.cluster.local:4317"
    }

    seeds = {
      "kafka/strimzi"       = { password = "" }
      "ui/nextauth"         = { secret = "" }
      "clickhouse/external" = { password = "" }
    }

    clickhouse_external_password = "contract-only-not-a-real-credential"
  }

  assert {
    condition     = module.managed_backends.clickhouse.service == ""
    error_message = "a host outside the vendor's domains is a server the deployer runs"
  }
}

run "external_carries_the_dials_own_ports_scheme_and_user" {
  command = plan

  variables {
    clickhouse = { mode = "external", port = 8123, native_port = 9000, secure = false, user = "dfe_writer" }

    endpoints = {
      clickhouse_host = "ch.example.com"
      kafka_bootstrap = ""
      otel_endpoint   = "otel-collector-gateway.otel.svc.cluster.local:4317"
    }

    seeds = {
      "kafka/strimzi"       = { password = "" }
      "ui/nextauth"         = { secret = "" }
      "clickhouse/external" = { password = "" }
    }

    clickhouse_external_password = "contract-only-not-a-real-credential"
  }

  assert {
    condition = (
      output.DFE_CLICKHOUSE_PORT == "8123" && output.DFE_CLICKHOUSE_NATIVE_PORT == "9000"
      && output.DFE_CLICKHOUSE_SECURE == "false" && output.DFE_CLICKHOUSE_USER == "dfe_writer"
    )
    error_message = "every external field the dial sets reaches its output unchanged"
  }
}

run "external_refuses_a_missing_password" {
  command = plan

  variables {
    clickhouse = { mode = "external" }

    endpoints = {
      clickhouse_host = "ch.example.com"
      kafka_bootstrap = ""
      otel_endpoint   = "otel-collector-gateway.otel.svc.cluster.local:4317"
    }
  }

  expect_failures = [
    var.clickhouse_external_password,
  ]
}

run "external_refuses_an_in_cluster_host" {
  command = plan

  variables {
    clickhouse                   = { mode = "external" }
    clickhouse_external_password = "contract-only-not-a-real-credential"
  }

  expect_failures = [
    var.clickhouse,
  ]
}

run "an_unknown_mode_is_refused" {
  command = plan

  variables {
    clickhouse = { mode = "cloud" }
  }

  expect_failures = [
    var.clickhouse,
  ]
}

run "a_port_out_of_range_is_refused" {
  command = plan

  variables {
    clickhouse = { mode = "external", port = 70000 }

    endpoints = {
      clickhouse_host = "ch.example.com"
      kafka_bootstrap = ""
      otel_endpoint   = "otel-collector-gateway.otel.svc.cluster.local:4317"
    }

    clickhouse_external_password = "contract-only-not-a-real-credential"
  }

  expect_failures = [
    var.clickhouse,
  ]
}

// Every NAT gateway's public address as a /32, whatever the edge module says:
// what a run opens an external service's access list to.
run "egress_cidrs_are_the_nat_addresses_as_slash_32s" {
  command = plan

  assert {
    condition     = output.DFE_EGRESS_CIDRS == "198.51.100.1/32,198.51.100.2/32"
    error_message = "DFE_EGRESS_CIDRS is every NAT public address as a /32, comma separated"
  }
}

// The ranges network-policies carves out of every internet allow.
run "cluster_ranges_are_the_vpc_and_the_service_range" {
  command = plan

  assert {
    condition     = output.DFE_POD_CIDR == "10.90.0.0/16" && output.DFE_SERVICE_CIDR == "172.20.0.0/16"
    error_message = "DFE_POD_CIDR is the VPC the CNI hands pods addresses out of, DFE_SERVICE_CIDR the cluster's Service range"
  }
}

// dfe-engine's store: the prefix its role is fenced to and the key it encrypts with.
run "the_engine_store_reaches_bootstrap" {
  command = plan

  assert {
    condition     = output.DFE_ENGINE_SECRETS_PREFIX == "dfe/test/engine"
    error_message = "DFE_ENGINE_SECRETS_PREFIX is <project>/<env>/engine, under the deployment's own path"
  }

  assert {
    condition     = output.DFE_KMS_KEY_ARN == "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000"
    error_message = "DFE_KMS_KEY_ARN is the deployment's own key, the cluster module's kms_key_arn"
  }

  assert {
    condition     = module.secrets.engine_name_prefix == output.DFE_ENGINE_SECRETS_PREFIX
    error_message = "the prefix bootstrap writes is the one the engine's role is fenced to"
  }
}

// dfe-ops teardown reads this to pass -suppress-forget-errors and say what stayed.
// The file's own tags are ephemeral, which keeps nothing.
run "an_ephemeral_destroy_keeps_nothing" {
  command = plan

  assert {
    condition     = output.retained_on_destroy == ""
    error_message = "an ephemeral deployment's destroy keeps nothing, so dfe-ops tears it down exactly as before"
  }
}

run "a_persistent_destroy_names_what_it_keeps" {
  command = plan

  variables {
    cloudtrail = { enabled = true }
    tags = {
      service-name      = "dfe"
      service-namespace = "example"
      environment       = "test"
      owner             = "owner@example.com"
      cost-center       = "experiments"
      lifecycle         = "persistent"
      iac-source        = "dfe-infra/terraform/environments/aws"
    }
  }

  assert {
    condition = jsondecode(output.retained_on_destroy) == {
      "module.cluster.aws_kms_key.retained[0]"                                  = "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000"
      "module.cluster.module.clickhouse_object_store.aws_s3_bucket.retained[0]" = "arn:aws:s3:::example-bucket"
      "module.cloudtrail_bucket[0].aws_s3_bucket.retained[0]"                   = module.cloudtrail_bucket[0].arn
      "module.toolbox_session_logs.aws_s3_bucket.retained[0]"                   = module.toolbox_session_logs.arn
    }
    error_message = "every resource a persistent destroy forgets is named by its address and ARN: the key and the ClickHouse, CloudTrail and session-log buckets"
  }

  assert {
    condition     = module.cloudtrail_bucket[0].retained && module.toolbox_session_logs.retained
    error_message = "a persistent deployment keeps its CloudTrail and session-log buckets through a destroy"
  }
}

run "an_ephemeral_deployment_destroys_its_buckets" {
  command = plan

  variables {
    cloudtrail = { enabled = true }
  }

  assert {
    condition     = !module.cloudtrail_bucket[0].retained && !module.toolbox_session_logs.retained
    error_message = "an ephemeral deployment destroys its CloudTrail and session-log buckets with their objects"
  }
}

// A test account's boundary refuses CreateSecret without both run tags, and the
// engine tags what it creates from this output, through the resource_tags annotation.
run "a_runs_tags_reach_the_engine_store" {
  command = plan

  variables {
    run = {
      id         = "r20261008t000000z-0a1b2c"
      expires_at = 1791460800
    }
  }

  assert {
    condition     = jsondecode(output.DFE_RESOURCE_TAGS)["dfe-e2e"] == "r20261008t000000z-0a1b2c"
    error_message = "DFE_RESOURCE_TAGS must carry the run id under dfe-e2e"
  }

  assert {
    condition     = jsondecode(output.DFE_RESOURCE_TAGS)["expires-at"] == "2026-10-08T12:00:00Z"
    error_message = "DFE_RESOURCE_TAGS must carry the run's expiry under expires-at, as YYYY-MM-DDTHH:MM:SSZ"
  }
}
