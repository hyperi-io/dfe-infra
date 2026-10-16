// The aws root's registry cache, executable: the dial's upstreams reach the
// registry-cache module, its node script reaches the managed node groups and
// the DFE_NODE_USER_DATA hand-off to Karpenter, and the cache off, the default,
// leaves both empty. Provider-free, like shapes.tftest.hcl beside it.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "000000000000"
    }
  }

  // The registry host the node script names is built from the partition's DNS suffix.
  mock_data "aws_partition" {
    defaults = {
      partition  = "aws"
      dns_suffix = "amazonaws.com"
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
    node_role_arn             = "arn:aws:iam::000000000000:role/dfe-example-nodes"

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

  edge       = { enabled = false, tunnel = { admin_peer = { enabled = false } } }
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

  // What render_dial.py renders for an aws dial that turns the cache on and names no credential.
  registry_cache = {
    enabled          = true
    pull_expiry_days = 90
    upstreams = {
      quay       = { upstream_url = "quay.io", host = "quay.io", requires_credential = false }
      k8s        = { upstream_url = "registry.k8s.io", host = "registry.k8s.io", requires_credential = false }
      ecr-public = { upstream_url = "public.ecr.aws", host = "public.ecr.aws", requires_credential = false }
    }
  }
}

run "the_dials_upstreams_reach_the_cache_and_the_nodes" {
  command = plan

  assert {
    condition     = length(module.registry_cache) == 1
    error_message = "an aws dial with the cache on must build the registry cache"
  }

  // namespace left empty takes the deployment's name, which no other deployment in the account shares.
  assert {
    condition     = module.registry_cache[0].mirrors["quay.io"] == "https://000000000000.dkr.ecr.us-west-2.amazonaws.com/v2/dfe-example/quay"
    error_message = "an empty namespace must take the deployment's name"
  }

  assert {
    condition     = strcontains(base64decode(output.DFE_NODE_USER_DATA), "mirror 'public.ecr.aws' 'https://public.ecr.aws'")
    error_message = "DFE_NODE_USER_DATA must carry the same mirror script the managed nodes run, for Karpenter's"
  }

  assert {
    condition     = toset(keys(output.registry_cache.mirrors)) == toset(["public.ecr.aws", "quay.io", "registry.k8s.io"])
    error_message = "with no credential named, the cache must mirror the three upstreams that need none, and docker.io and ghcr.io must pull directly"
  }

  assert {
    condition     = output.registry_cache.uncached == []
    error_message = "the dial renders no credentialed upstream it has no credential for, so nothing is reported skipped"
  }

  assert {
    condition     = module.registry_cache[0].registry == output.registry_cache.registry
    error_message = "the operator output must name the registry the nodes pull through"
  }
}

// What render_dial.py renders for an aws dial that says nothing about the cache.
run "the_cache_is_off_by_default" {
  command = plan

  variables {
    registry_cache = {
      pull_expiry_days = 90
      upstreams = {
        quay = { upstream_url = "quay.io", host = "quay.io", requires_credential = false }
      }
    }
  }

  assert {
    condition     = length(module.registry_cache) == 0
    error_message = "a dial that does not turn the cache on must build no cache"
  }

  assert {
    condition     = output.DFE_NODE_USER_DATA == ""
    error_message = "with the cache off by default no node may run a mirror script"
  }
}

// A hand-written tfvars can still name a credentialed upstream with no ARN; the module skips it.
run "an_upstream_needing_a_credential_without_one_is_pulled_directly" {
  command = plan

  variables {
    registry_cache = {
      enabled          = true
      pull_expiry_days = 90
      upstreams = {
        docker-hub = { upstream_url = "registry-1.docker.io", host = "docker.io", requires_credential = true }
        quay       = { upstream_url = "quay.io", host = "quay.io", requires_credential = false }
      }
    }
  }

  assert {
    condition     = output.registry_cache.uncached == ["docker-hub"]
    error_message = "an upstream with no credential must be reported as pulled directly"
  }

  assert {
    condition     = !contains(keys(output.registry_cache.mirrors), "docker.io")
    error_message = "no node may mirror an upstream the cache has no rule for"
  }
}

// scripts/cloud_sweep.py selects a test run's cached repositories by these two tags.
run "a_test_runs_cached_repositories_carry_its_run_tags" {
  command = plan

  variables {
    run = {
      id         = "r20261008t000000z-0a1b2c"
      expires_at = 1791460800
    }
  }

  assert {
    condition     = toset(keys(module.registry_cache[0].repository_tags)) == toset(["ecr-public", "k8s", "quay"])
    error_message = "every cached upstream must have a creation template"
  }

  assert {
    condition     = alltrue([for tags in values(module.registry_cache[0].repository_tags) : lookup(tags, "dfe-e2e", "") == "r20261008t000000z-0a1b2c" && lookup(tags, "expires-at", "") == "2026-10-08T12:00:00Z"])
    error_message = "every template must stamp the run's dfe-e2e and expires-at tags on the repositories ECR creates"
  }
}

run "a_namespace_from_the_dial_wins" {
  command = plan

  variables {
    registry_cache = {
      enabled          = true
      namespace        = "dfe-blue"
      pull_expiry_days = 90
      upstreams = {
        quay = { upstream_url = "quay.io", host = "quay.io", requires_credential = false }
      }
    }
  }

  assert {
    condition     = module.registry_cache[0].mirrors["quay.io"] == "https://000000000000.dkr.ecr.us-west-2.amazonaws.com/v2/dfe-blue/quay"
    error_message = "a dial namespace must replace the deployment name as the rule prefix"
  }
}

run "the_cache_off_hands_karpenter_nothing" {
  command = plan

  variables {
    registry_cache = {
      enabled          = false
      pull_expiry_days = 90
      upstreams = {
        quay = { upstream_url = "quay.io", host = "quay.io", requires_credential = false }
      }
    }
  }

  assert {
    condition     = length(module.registry_cache) == 0
    error_message = "enabled = false must build no cache"
  }

  assert {
    condition     = output.DFE_NODE_USER_DATA == ""
    error_message = "with the cache off no node may run a mirror script"
  }
}

run "upstreams_without_an_expiry_are_refused" {
  command = plan

  variables {
    registry_cache = {
      enabled = true
      upstreams = {
        quay = { upstream_url = "quay.io", host = "quay.io", requires_credential = false }
      }
    }
  }

  expect_failures = [var.registry_cache]
}
