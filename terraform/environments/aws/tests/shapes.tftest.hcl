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

  // CloudTrail refuses a log group ARN that is not one.
  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      arn = "arn:aws:logs:us-west-2:000000000000:log-group:/aws/cloudtrail/mock"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::000000000000:role/mock"
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

  // What scripts/resolve_sizing.py images writes into node-images.auto.tfvars.json.
  node_image       = { family = "AL2023_ARM_64_STANDARD", version = "1.36.4-20261012" }
  instance_ami_id  = "ami-0123456789abcdef0"
  forwarder_ami_id = "ami-0123456789abcdef1"

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

// With no AMI from the resolver or tfvars, the forwarder is refused by name.
run "a_forwarder_refuses_an_unresolved_ami" {
  command = plan

  variables {
    forwarder_ami_id = null
  }

  expect_failures = [var.forwarder_ami_id]
}

// The forwarder reads its own key, so the toolbox-only refresh bastion up runs
// never reaches it.
run "the_forwarder_launches_without_the_toolbox_ami" {
  command = plan

  variables {
    instance_ami_id = null
  }

  assert {
    condition     = length(module.edge[0].tunnel_listener_ports) > 0
    error_message = "the forwarder must plan on forwarder_ami_id alone, with the toolbox off and its AMI unresolved"
  }
}

run "a_toolbox_refuses_an_unresolved_ami" {
  command = plan

  variables {
    instance_ami_id = null
    toolbox = {
      enabled                    = true
      aws                        = { shape_ref = "toolbox", operator_role_arn = "arn:aws:iam::000000000000:role/example-operator" }
      ttl_minutes                = 60
      session                    = { idle_timeout_minutes = 15, max_duration_minutes = 240 }
      session_log_retention_days = 90
      tool_versions = {
        kubectl                    = "1"
        helm                       = "1"
        argocd-cli                 = "1"
        tofu                       = "1"
        yq                         = "1"
        aws-cli                    = "1"
        aws-session-manager-plugin = "1"
        clickhouse-client          = "1"
        psql                       = "1"
        kafka-cli                  = "1"
        argocd-cli-arm64-sha256    = "1111111111111111111111111111111111111111111111111111111111111111"
        yq-arm64-sha256            = "2222222222222222222222222222222222222222222222222222222222222222"
        kafka-cli-signer           = "1:3333333333333333333333333333333333333333"
      }
    }
  }

  expect_failures = [var.instance_ami_id]
}

// A group with no version would take EKS's newest release ungated, so neither an
// absent image nor one without a release plans.
run "a_plan_with_no_resolved_node_image_is_refused" {
  command = plan

  variables {
    node_image = null
  }

  expect_failures = [var.node_image]
}

run "the_mesh_profile_is_refused" {
  command = plan

  variables {
    profile = "mesh"
  }

  expect_failures = [var.profile]
}

run "a_node_image_with_no_release_is_refused" {
  command = plan

  variables {
    node_image = { family = "AL2023_ARM_64_STANDARD" }
  }

  expect_failures = [var.node_image]
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
      provider             = "confluent-cloud"
      connectivity         = "public"
      message_max_bytes    = 8388608
      log_retention_ms     = 259200000
      confluent_rest_ready = { successes = 12, timeout_seconds = 900, interval_seconds = 5 }
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
      message_max_bytes = 16777216
      log_retention_ms  = 259200000
    }

    seeds = {
      "kafka/redpanda-cloud" = { password = "" }
      "ui/nextauth"          = { secret = "" }
    }
  }

  expect_failures = [var.kafka]
}

// The collector's provider-logs workload reads the EKS control-plane group and
// CloudTrail's through one Pod Identity role, and nothing else.
run "the_provider_logs_role_reads_exactly_the_eks_and_cloudtrail_groups" {
  command = plan

  variables {
    cloudtrail = { enabled = true }
  }

  assert {
    condition     = length(aws_eks_pod_identity_association.provider_logs) == 1
    error_message = "sink = otel must bind the provider-logs account to a Pod Identity role"
  }

  assert {
    condition     = aws_eks_pod_identity_association.provider_logs[0].namespace == "otel" && aws_eks_pod_identity_association.provider_logs[0].service_account == "dfe-otel-collector-provider-logs"
    error_message = "the association must name the chart's provider-logs account in the otel namespace"
  }

  assert {
    condition     = sort(local.provider_logs_group_names) == sort(["/aws/eks/dfe-example/cluster", "/aws/cloudtrail/dfe-example-cloudtrail"])
    error_message = "the role must be scoped to the EKS and CloudTrail log groups"
  }

  assert {
    condition     = alltrue([for arn in local.provider_logs_group_arns : startswith(arn, "arn:aws:logs:us-west-2:000000000000:log-group:/aws/")])
    error_message = "every ARN the role reads must be a log group in this account and region"
  }

  assert {
    condition     = output.DFE_PROVIDER_LOGS_FLAVOUR == "aws"
    error_message = "the flavour fact must say aws so the chart renders the receiver"
  }

  assert {
    condition     = aws_cloudwatch_log_group.cloudtrail[0].retention_in_days == var.telemetry.forced_retention_days
    error_message = "under sink = otel CloudTrail's log group keeps only the forced retention, since the otel database holds the history"
  }
}

// With the trail disabled the role covers the EKS group alone.
run "the_provider_logs_role_drops_cloudtrail_when_the_trail_is_off" {
  command = plan

  assert {
    condition     = length(local.provider_logs_group_names) == 1 && local.provider_logs_group_names[0] == "/aws/eks/dfe-example/cluster"
    error_message = "no trail means no CloudTrail group to read"
  }

  assert {
    condition     = output.DFE_CLOUDTRAIL_LOG_GROUP == ""
    error_message = "the CloudTrail log group fact must be empty when the trail is off"
  }
}

// DFE reads neither group into its own database under the AWS-native sink.
run "the_cloudwatch_sink_builds_no_provider_logs_role" {
  command = plan

  variables {
    cloudtrail = { enabled = true }
    telemetry  = { sink = "cloudwatch", retention_days = 7, forced_retention_days = 1, eks_log_types = ["audit"] }
  }

  assert {
    condition     = length(aws_iam_role.provider_logs) == 0 && length(aws_eks_pod_identity_association.provider_logs) == 0
    error_message = "sink = cloudwatch must build no provider-logs role"
  }

  assert {
    condition     = output.DFE_PROVIDER_LOGS_FLAVOUR == ""
    error_message = "the flavour fact must be empty so the chart renders nothing"
  }

  assert {
    condition     = aws_cloudwatch_log_group.cloudtrail[0].retention_in_days == 7
    error_message = "under sink = cloudwatch CloudTrail's log group keeps telemetry.retention_days"
  }
}
