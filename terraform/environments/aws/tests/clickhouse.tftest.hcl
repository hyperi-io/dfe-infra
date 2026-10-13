// The aws root's external ClickHouse wiring, executable: the facts it hands
// bootstrap for each mode, the shapes it refuses before anything is planned, and
// the cluster's egress addresses.
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

    pod_identity_trust_policy_json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"

    audit_log_group                  = "/aws/eks/dfe-example/cluster"
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

override_module {
  target = module.toolbox

  outputs = {
    instance_id          = ""
    ssm_session_document = ""
    targets              = {}
    session_log_bucket   = "example-toolbox-session-logs"
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
    }
  }

  network  = { nat = "single" }
  endpoint = { public = false, allowed_cidrs = [] }
  dns      = { private_zone = "dfe-example.internal", public_zone = "" }

  storage_class = "gp3"

  edge       = { enabled = false }
  cloudtrail = { enabled = false }

  kafka = { provider = "strimzi" }

  toolbox = {
    enabled     = false
    aws         = { instance_type = "t4g.small" }
    ttl_minutes = 60
    session     = { idle_timeout_minutes = 15, max_duration_minutes = 240 }
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
