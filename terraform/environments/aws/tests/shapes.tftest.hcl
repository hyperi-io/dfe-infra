// The aws root's sizing inputs, executable: the toolbox and the tunnel
// forwarder take their instance type and root volume off a resolved shape, and
// every value that used to default in a module arrives in tfvars or is refused.
//
// Provider-free by construction, like kafka.tftest.hcl beside it. The toolbox
// and the edge module are planned for real, so their own validation of the type
// and the root volume they are handed runs here too.

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

  // A generated policy document or ARN is a random string the provider refuses.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  mock_resource "aws_iam_policy" {
    defaults = {
      arn = "arn:aws:iam::000000000000:policy/mock"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::000000000000:role/mock"
    }
  }

  mock_data "aws_ssm_parameter" {
    defaults = {
      value = "ami-0123456789abcdef0"
    }
  }

  // The toolbox refuses a tunnel address that is not an IPv4 address.
  mock_resource "aws_eip" {
    defaults = {
      id        = "eipalloc-0000000000000000f"
      public_ip = "198.51.100.42"
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
    nat_public_ips            = ["198.51.100.1"]

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

  // What scripts/resolve_sizing.py writes into sizing.auto.tfvars.json.
  resolved_shapes = {
    eks-system = {
      instance_types = ["m9g.large"]
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
  telemetry = { sink = "otel", retention_days = 2, forced_retention_days = 1 }

  storage_class = "gp3"

  edge = {
    enabled = true
    tunnel = {
      address     = { mode = "forwarder", shape_ref = "toolbox" }
      client_cidr = "100.64.0.0/10"
    }
  }
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
    kafka_bootstrap = "dfe-kafka-bootstrap.strimzi.svc.cluster.local:9092"
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

// The edge module refuses a forwarder whose type is not arm64 or whose root
// volume is missing, and the toolbox module the same for its own pair, so a
// clean plan proves both took the resolved shape's answer.
run "the_toolbox_and_the_forwarder_launch_on_the_resolved_shape" {
  command = plan

  assert {
    condition     = length(module.edge[0].tunnel_listener_ports) > 0
    error_message = "address.mode forwarder must build the forwarder from the resolved shape"
  }
}

run "a_forwarder_refuses_a_shape_the_resolver_did_not_write" {
  command = plan

  variables {
    edge = {
      enabled = true
      tunnel = {
        address     = { mode = "forwarder", shape_ref = "no-such-shape" }
        client_cidr = "100.64.0.0/10"
      }
    }
  }

  expect_failures = [var.edge]
}

run "a_toolbox_refuses_a_shape_with_no_root_volume" {
  command = plan

  variables {
    resolved_shapes = {
      eks-system = {
        instance_types = ["m9g.large"]
        arch           = "arm64"
        root_volume    = { type = "gp3", size_gib = 40 }
      }
      toolbox = {
        instance_types = ["t4g.small"]
        arch           = "arm64"
      }
    }
  }

  expect_failures = [var.toolbox, var.edge]
}

run "the_reach_back_refuses_a_missing_client_range" {
  command = plan

  variables {
    edge = { enabled = false }
  }

  expect_failures = [var.edge]
}

run "a_confluent_broker_refuses_a_missing_tier" {
  command = plan

  variables {
    kafka = {
      provider          = "confluent-cloud"
      connectivity      = "public"
      num_partitions    = 12
      log_retention_ms  = 259200000
      message_max_bytes = 8388608
      landing_topics    = { main_land = {} }
    }

    seeds = {
      "kafka/confluent-cloud" = { username = "", password = "" }
      "ui/nextauth"           = { secret = "" }
    }
  }

  expect_failures = [var.kafka]
}

run "a_redpanda_broker_refuses_a_missing_tier" {
  command = plan

  variables {
    kafka = {
      provider          = "redpanda-cloud"
      connectivity      = "public"
      num_partitions    = 12
      log_retention_ms  = 259200000
      message_max_bytes = 16777216
      landing_topics    = { main_land = {} }
    }

    seeds = {
      "kafka/redpanda-cloud" = { password = "" }
      "ui/nextauth"          = { secret = "" }
    }
  }

  expect_failures = [var.kafka]
}
