// The aws root's managed-Kafka wiring, executable: what the root hands each SaaS
// body, and the message size it refuses before any body is planned.
//
// Provider-free by construction: every provider is mocked and every module this
// file does not exercise is overridden, so it needs no credential and no cloud
// account. The SaaS bodies themselves are planned for real against the mocks,
// because what they receive is what this file is here to prove.

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

// The same IDs and endpoints the managed-kafka contract tests mock, so a
// bootstrap string reaching the root is one those tests already normalise.
mock_provider "confluent" {
  mock_resource "confluent_environment" {
    defaults = {
      id = "env-000000"
    }
  }

  mock_resource "confluent_kafka_cluster" {
    defaults = {
      id          = "lkc-000000"
      api_version = "cmk/v2"
      kind        = "Cluster"

      bootstrap_endpoint = "SASL_SSL://pkc-00000.us-west-2.aws.confluent.cloud:9092"
      rest_endpoint      = "https://pkc-00000.us-west-2.aws.confluent.cloud:443"
      rbac_crn           = "crn://confluent.cloud/organization=00000000-0000-0000-0000-000000000000/environment=env-000000/cloud-cluster=lkc-000000"
    }
  }

  mock_resource "confluent_service_account" {
    defaults = {
      id          = "sa-000000"
      api_version = "iam/v2"
      kind        = "ServiceAccount"
    }
  }

  mock_resource "confluent_api_key" {
    defaults = {
      id     = "AAAAAAAAAAAAAAAA"
      secret = "contract-only-not-a-real-credential"
    }
  }
}

mock_provider "redpanda" {
  mock_resource "redpanda_resource_group" {
    defaults = {
      id = "00000000-0000-0000-0000-000000000000"
    }
  }

  mock_resource "redpanda_serverless_private_link" {
    defaults = {
      id = "aaaaaaaaaaaaaaaaaaaa"

      status = {
        aws = {
          availability_zones        = ["us-west-2a", "us-west-2b", "us-west-2c"]
          vpc_endpoint_service_name = "com.amazonaws.vpce.us-west-2.vpce-svc-00000000000000000"
        }
      }
    }
  }

  mock_resource "redpanda_serverless_cluster" {
    defaults = {
      id = "cccccccccccccccccccc"

      kafka_api = {
        seed_brokers         = ["seed-0000.any.us-west-2.aw.prd.cloud.redpanda.com:9092"]
        private_seed_brokers = ["seed-0000.any.us-west-2.aw.priv.prd.cloud.redpanda.com:9092"]
      }

      dataplane_api = {
        url         = "https://api-0000.any.us-west-2.aw.prd.cloud.redpanda.com"
        private_url = "https://api-0000.any.us-west-2.aw.priv.prd.cloud.redpanda.com"
      }
    }
  }

  mock_resource "redpanda_user" {
    defaults = {
      id = "dfe-kafka-user"
    }
  }
}

// The cluster and the toolbox are contract-tested in their own modules; here
// they only have to hand the root the outputs it reads.
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

// The store is planned for real, because its own validation refuses a kafka seed
// that asks for a username the root did not hand it. Only the values the aws
// provider checks the shape of are pinned, for each of its three roles.
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

  edge       = { enabled = false, tunnel = { client_cidr = "100.64.0.0/10" } }
  cloudtrail = { enabled = false }

  kafka = {
    provider             = "confluent-cloud"
    connectivity         = "public"
    confluent_tier       = "basic"
    message_max_bytes    = 8388608
    confluent_rest_ready = { successes = 12, timeout_seconds = 900, interval_seconds = 5 }
  }

  toolbox = {
    enabled                    = false
    aws                        = { shape_ref = "toolbox" }
    ttl_minutes                = 60
    session                    = { idle_timeout_minutes = 15, max_duration_minutes = 240 }
    session_log_retention_days = 90
  }

  secrets = { backend = "aws-sm", ref = "" }

  seeds = {
    "kafka/confluent-cloud" = { username = "", password = "" }
    "ui/nextauth"           = { secret = "" }
  }

  endpoints = {
    clickhouse_host = "dfe-clickhouse.clickhouse.svc.cluster.local"
    kafka_bootstrap = ""
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

// Confluent Basic, public, at its own 8 MiB ceiling. The body refuses basic on
// private and freight on public, and the store refuses a username seed with no
// username, so a clean plan is itself proof that the tier and connectivity
// reached the body and its API key reached the store.
run "confluent_basic_public_reaches_the_body" {
  command = plan

  assert {
    condition     = length(module.confluent[0].network_attachment.subnet_ids) == 0 && module.confluent[0].network_attachment.security_group_id == ""
    error_message = "connectivity = public must reach the confluent-cloud body, which then builds no private attachment"
  }

  assert {
    condition     = output.DFE_KAFKA_BOOTSTRAP == "pkc-00000.us-west-2.aws.confluent.cloud:9092"
    error_message = "the root must hand DFE the body's bare bootstrap"
  }

  assert {
    condition     = output.DFE_KAFKA_MESSAGE_MAX_BYTES == "8388608"
    error_message = "the dial's message size is the size the engine creates the topics at"
  }

  // The body refuses any cloud token but AWS, so a clean plan is proof the
  // offering's token reached it.
  assert {
    condition     = module.managed_backends.kafka.vendor_cloud == "AWS"
    error_message = "an aws deployment's Confluent cluster is Confluent's AWS instance"
  }
}

// Above Basic's ceiling Confluent refuses the topics dfe-engine creates, after
// the cluster exists. The root refuses first.
run "confluent_basic_refuses_a_message_size_above_8_mib" {
  command = plan

  variables {
    kafka = {
      provider             = "confluent-cloud"
      connectivity         = "public"
      confluent_tier       = "basic"
      message_max_bytes    = 16777216
      confluent_rest_ready = { successes = 12, timeout_seconds = 900, interval_seconds = 5 }
    }
  }

  expect_failures = [
    var.kafka,
  ]
}

// The body defaults no part of its REST endpoint wait, so the root refuses a
// confluent-cloud deployment that arrives without one.
run "confluent_refuses_a_deployment_with_no_rest_wait" {
  command = plan

  variables {
    kafka = {
      provider          = "confluent-cloud"
      connectivity      = "public"
      confluent_tier    = "basic"
      message_max_bytes = 8388608
    }
  }

  expect_failures = [
    var.kafka,
  ]
}

// dfe-engine creates a SaaS body's topics at the dial's size, so the root
// refuses a deployment that arrives without one.
run "a_saas_body_refuses_a_deployment_with_no_message_size" {
  command = plan

  variables {
    kafka = {
      provider      = "redpanda-cloud"
      redpanda_tier = "serverless"
    }

    seeds = {
      "kafka/redpanda-cloud" = { password = "" }
      "ui/nextauth"          = { secret = "" }
    }
  }

  expect_failures = [
    var.kafka,
  ]
}

// Enterprise carries the chain's 16 MiB, so the same size is not refused there.
run "confluent_enterprise_keeps_16_mib" {
  command = plan

  variables {
    kafka = {
      provider             = "confluent-cloud"
      connectivity         = "public"
      confluent_tier       = "enterprise"
      message_max_bytes    = 16777216
      confluent_rest_ready = { successes = 12, timeout_seconds = 900, interval_seconds = 5 }
    }
  }

  assert {
    condition     = output.DFE_KAFKA_MESSAGE_MAX_BYTES == "16777216"
    error_message = "a tier whose ceiling is 20 MiB must carry the 16 MiB chain unchanged"
  }
}

// Redpanda on public connectivity. A Confluent tier left in the dial caps
// nothing here: the ceiling is Confluent's.
run "redpanda_public_reaches_the_body" {
  command = plan

  variables {
    kafka = {
      provider                   = "redpanda-cloud"
      connectivity               = "public"
      confluent_tier             = "basic"
      redpanda_tier              = "serverless"
      redpanda_resource_group_id = "11111111-2222-3333-4444-555555555555"
      message_max_bytes          = 16777216
    }

    seeds = {
      "kafka/redpanda-cloud" = { password = "" }
      "ui/nextauth"          = { secret = "" }
    }
  }

  assert {
    condition     = output.DFE_KAFKA_BOOTSTRAP == "seed-0000.any.us-west-2.aw.prd.cloud.redpanda.com:9092"
    error_message = "connectivity = public must reach the redpanda-cloud body, which then hands out its public seed brokers"
  }

  assert {
    condition     = module.redpanda[0].network_attachment.security_group_id == ""
    error_message = "a public Redpanda cluster builds no PrivateLink endpoint"
  }

  assert {
    condition     = module.managed_backends.kafka.vendor_cloud == "aws" && module.managed_backends.kafka.client_ports == tolist([9092, 9093, 9094, 9095])
    error_message = "an aws deployment's Redpanda cluster is Serverless on aws, dialled on its seed and broker ports"
  }
}

// MSK's own AmazonMSK_ secret is the one copy of the credential: the store gets
// no kafka seed, and the root names that secret for ESO to read.
run "msk_keeps_one_copy_of_the_credential" {
  command = plan

  override_module {
    target = module.kafka

    outputs = {
      bootstrap               = "b-1.example.invalid:9096"
      bootstrap_port          = 9096
      bootstrap_iam           = "b-1.example.invalid:9098"
      auth_type               = "scram"
      credential_ref          = "arn:aws:secretsmanager:us-west-2:000000000000:secret:AmazonMSK_dfe-example-AbCdEf"
      network_attachment      = { security_group_id = "sg-00000000000000001", subnet_ids = [] }
      cluster_arn             = "arn:aws:kafka:us-west-2:000000000000:cluster/dfe-example/00000000-0000-0000-0000-000000000000-1"
      bootstrap_role_arn      = "arn:aws:iam::000000000000:role/dfe-example-kafka-bootstrap"
      autoscaler_alarm_arn    = ""
      autoscaler_function_arn = ""
      broker_log_bucket       = ""
    }
  }

  variables {
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
      msk-broker = {
        instance_types = ["express.m7g.large"]
        arch           = "arm64"
      }
    }

    kafka = {
      provider = "msk"
      msk = {
        shape_ref         = "msk-broker"
        broker_count      = 3
        broker_version    = "4.2.x.kraft"
        num_partitions    = 12
        log_retention_ms  = 259200000
        message_max_bytes = 16777216
        scram_username    = "dfe-kafka-user"
        bootstrap_job     = { namespace = "strimzi", service_account = "dfe-kafka-bootstrap" }
        autoscaling = {
          max_brokers              = 6
          per_broker_capacity_mb_s = 50
          headroom                 = 1.3
          period_seconds           = 300
          evaluation_periods       = 3
        }
      }
    }

    seeds = {
      "ui/nextauth" = { secret = "" }
    }
  }

  assert {
    condition     = output.DFE_KAFKA_CREDENTIAL_REF == "arn:aws:secretsmanager:us-west-2:000000000000:secret:AmazonMSK_dfe-example-AbCdEf"
    error_message = "the root names MSK's own secret, which the apps' copy reads"
  }

  assert {
    condition     = output.DFE_KAFKA_PROVIDER == "msk" && output.DFE_SECRETS_BACKEND == "aws-sm"
    error_message = "an msk deployment keeps its secrets in Secrets Manager"
  }
}

// Private stays the default for every deployment that does not ask.
run "an_unstated_connectivity_stays_private" {
  command = plan

  variables {
    kafka = {
      provider          = "redpanda-cloud"
      redpanda_tier     = "serverless"
      message_max_bytes = 16777216
    }

    seeds = {
      "kafka/redpanda-cloud" = { password = "" }
      "ui/nextauth"          = { secret = "" }
    }
  }

  assert {
    condition     = output.DFE_KAFKA_BOOTSTRAP == "seed-0000.any.us-west-2.aw.priv.prd.cloud.redpanda.com:9092"
    error_message = "with no connectivity in the dial the body builds the private shape"
  }
}
