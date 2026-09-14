// The contract, executable -- the redpanda-cloud/ body's half of it. Same
// assertions as contract.tftest.hcl makes of msk/, against the same variables.
// The header of that file says why each body gets its own.
//
// Provider-free by construction: mock_provider means no credentials, no API
// call and no cost, so it runs in CI and on a laptop with no cloud account.

mock_provider "aws" {
  // The partition is half of the principal ARN Redpanda is handed, and the
  // provider validates an ARN's shape before it ever calls AWS.
  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }
}

// Redpanda's control plane returns a UUID for a resource group and a
// 20-character handle for a private link, and the provider validates the shape
// of both before it calls the API.
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

  // Redpanda returns the seed brokers as a LIST, and a private cluster returns
  // a different list from a public one. Supplying both is what makes the
  // normalised bootstrap output testable.
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

variables {
  name = "dfe-kafka-contract"
  env  = "test"

  network = {
    vpc_id             = "vpc-00000000000000000"
    cidr               = "10.90.0.0/16"
    azs                = ["mock-1a", "mock-1b", "mock-1c"]
    private_subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002", "subnet-00000000000000003"]
    public_subnet_ids  = ["subnet-00000000000000004", "subnet-00000000000000005", "subnet-00000000000000006"]
  }

  scram_password = "contract-only-not-a-real-credential"

  // No module default: the same numbers a customer dial carries, so a
  // contract run proves what render_dial.py actually sends.
  num_partitions    = 12
  log_retention_ms  = 259200000
  message_max_bytes = 16777216

  landing_topics = {
    dfe_land = {}
    dfe_dlq  = { retention_ms = 604800000 }
  }
}

run "redpanda_cloud_contract_outputs" {
  command = plan

  module {
    source = "./redpanda-cloud"
  }

  assert {
    condition     = can(tostring(output.bootstrap))
    error_message = "bootstrap must be a string"
  }

  // Bare host:port pairs -- no scheme, no trailing comma, no list. Redpanda
  // returns a list, so this is the assertion that proves the normalising.
  assert {
    condition     = !strcontains(output.bootstrap, "://") && !endswith(output.bootstrap, ",")
    error_message = "bootstrap must be bare host:port pairs, comma separated"
  }

  assert {
    condition     = strcontains(output.bootstrap, ":9092")
    error_message = "bootstrap must be the Kafka API endpoint, which Redpanda serves on 9092"
  }

  assert {
    condition     = output.bootstrap_port == 9092
    error_message = "bootstrap_port must be the Kafka API client port, a literal known at plan time"
  }

  // Private connectivity is the default, so the PRIVATE seed brokers are what a
  // caller gets without asking for anything.
  assert {
    condition     = strcontains(output.bootstrap, ".priv.")
    error_message = "on private connectivity the bootstrap must be the private seed brokers, not the public ones"
  }

  assert {
    condition     = output.auth_type == "scram"
    error_message = "auth_type must be scram -- Redpanda Cloud is SCRAM-SHA-512"
  }

  // A reference, never a value.
  assert {
    condition     = output.credential_ref != "" && output.credential_ref != var.scram_password
    error_message = "credential_ref must be the Redpanda user's ID, never the password"
  }

  assert {
    condition     = length(output.network_attachment.subnet_ids) == 3
    error_message = "network_attachment.subnet_ids must carry the private subnet per zone the endpoint lives in"
  }

  assert {
    condition     = can(tostring(output.network_attachment.security_group_id))
    error_message = "network_attachment.security_group_id must be a string"
  }

  assert {
    condition     = output.cluster_id != ""
    error_message = "cluster_id must be the vendor's handle on the cluster"
  }

  // Redpanda Cloud has neither an IAM endpoint nor an in-cluster bootstrap Job,
  // and the contract says a body with no such mechanism returns empty.
  assert {
    condition     = output.bootstrap_iam == "" && output.bootstrap_role_arn == ""
    error_message = "a body with no IAM mechanism must return empty for bootstrap_iam and bootstrap_role_arn"
  }
}

// Private by default: the endpoint exists and the public half of the cluster's
// networking is off, with nobody having asked for either.
run "redpanda_cloud_private_by_default" {
  command = plan

  module {
    source = "./redpanda-cloud"
  }

  assert {
    condition     = redpanda_serverless_cluster.this.networking_config.private == "STATE_ENABLED"
    error_message = "private networking must be enabled on the cluster"
  }

  assert {
    condition     = redpanda_serverless_cluster.this.networking_config.public == "STATE_DISABLED"
    error_message = "the public endpoint must be off when connectivity is private"
  }

  assert {
    condition     = length(redpanda_serverless_private_link.this) == 1
    error_message = "private connectivity must create the PrivateLink the cluster attaches to"
  }

  assert {
    condition     = aws_vpc_endpoint.this[0].vpc_endpoint_type == "Interface"
    error_message = "the AWS end of the handshake must be an interface endpoint"
  }

  // The endpoint service publishes a verified private DNS name, and this is
  // what makes the seed brokers resolve inside the VPC with no zone of ours.
  assert {
    condition     = aws_vpc_endpoint.this[0].private_dns_enabled == true
    error_message = "private DNS must be enabled on the endpoint"
  }
}

// Public is a shape you have to ask for, and asking for it moves the bootstrap
// to the public seed brokers and builds no endpoint.
run "redpanda_cloud_public_when_asked" {
  command = plan

  module {
    source = "./redpanda-cloud"
  }

  variables {
    connectivity = "public"
  }

  assert {
    condition     = length(redpanda_serverless_private_link.this) == 0
    error_message = "public connectivity must not create a PrivateLink"
  }

  assert {
    condition     = redpanda_serverless_cluster.this.networking_config.public == "STATE_ENABLED"
    error_message = "public connectivity must enable the cluster's public endpoint"
  }

  assert {
    condition     = output.network_attachment.security_group_id == ""
    error_message = "there is no cloud-side attachment to report when the cluster is public"
  }
}

// One principal at the on-prem grant set: topic * literal with Read, Write,
// Create and Describe, and groups fenced to the dfe- prefix with Read and
// Describe. Widening it here would make the cloud path looser than ours.
run "redpanda_cloud_acls_at_on_prem_parity" {
  command = plan

  module {
    source = "./redpanda-cloud"
  }

  assert {
    condition     = length(redpanda_acl.topics) == 4
    error_message = "the topic grant must be exactly Read, Write, Create and Describe"
  }

  assert {
    condition     = alltrue([for acl in values(redpanda_acl.topics) : acl.resource_name == "*" && acl.resource_pattern_type == "LITERAL"])
    error_message = "the topic grant must be topic * LITERAL, as kafka-user.yaml gives on-prem"
  }

  assert {
    condition     = length(redpanda_acl.groups) == 2
    error_message = "the consumer group grant must be exactly Read and Describe"
  }

  assert {
    condition     = alltrue([for acl in values(redpanda_acl.groups) : acl.resource_name == "dfe-" && acl.resource_pattern_type == "PREFIXED"])
    error_message = "consumer groups must be fenced to the dfe- prefix, not allowed wholesale"
  }

  assert {
    condition     = redpanda_user.this.mechanism == "scram-sha-512"
    error_message = "the user must be SCRAM-SHA-512, matching the on-prem principal"
  }

  // Pre-created, because dfe-loader treats a missing landing topic as fatal and
  // Redpanda Cloud accepts no auto-create setting on any tier.
  assert {
    condition     = length(redpanda_topic.landing) == 2
    error_message = "every landing topic must be pre-created"
  }
}

// Dedicated and BYOC are Request-access and quote-only, so they fail at plan
// rather than building something nobody can price.
run "redpanda_cloud_rejects_dedicated" {
  command = plan

  module {
    source = "./redpanda-cloud"
  }

  variables {
    tier = "dedicated"
  }

  expect_failures = [
    var.tier,
  ]
}

run "redpanda_cloud_rejects_byoc" {
  command = plan

  module {
    source = "./redpanda-cloud"
  }

  variables {
    tier = "byoc"
  }

  expect_failures = [
    var.tier,
  ]
}
