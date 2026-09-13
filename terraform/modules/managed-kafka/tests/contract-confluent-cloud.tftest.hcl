// The contract, executable -- the confluent-cloud/ body's half of it. Same
// assertions as contract.tftest.hcl makes of msk/, against the same variables.
// The header of that file says why each body gets its own.
//
// Provider-free by construction: mock_provider means no credentials, no API
// call and no cost, so it runs in CI and on a laptop with no cloud account.

mock_provider "aws" {
  // The account the interfaces live in, which the access point has to name.
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "000000000000"
    }
  }

  // A zone ID is not derivable from a zone name, and the gateway is placed by
  // ID rather than name because the same zone carries a different name in each
  // account.
  mock_data "aws_availability_zone" {
    defaults = {
      zone_id = "mock-az1"
    }
  }

  // The endpoint's own DNS name is what every record in the private hosted zone
  // points at, and it is computed.
  mock_resource "aws_vpc_endpoint" {
    defaults = {
      id = "vpce-00000000000000000"

      dns_entry = [{
        dns_name       = "vpce-0000-aaaa.vpce-svc-0000.us-west-2.vpce.amazonaws.com"
        hosted_zone_id = "Z0000000000000000000"
      }]
    }
  }

  mock_resource "aws_route53_zone" {
    defaults = {
      zone_id = "Z0000000000000000001"
    }
  }

  // aws_network_interface gets no mock_resource default, and the Freight runs
  // below supply the interface IDs instead of having the module create them.
  // A mock default is shared by EVERY instance of a type, and the access point
  // takes the interfaces as a SET, so one ID repeated collapses to a single
  // item and the provider refuses it for being under its six-interface floor.
  // override_resource cannot fix it either -- it addresses a resource, not an
  // instance. `tofu validate` passes on the module-created path, where the IDs
  // are genuinely unknown rather than identical, and S3.T3b's apply is what
  // settles it against the real API.
}

// Confluent's IDs carry their own prefixes -- env-, lkc-, sa-, gw-, platt- --
// and the api_version and kind of each are how one resource references another.
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

  mock_resource "confluent_private_link_attachment" {
    defaults = {
      id         = "platt-000000"
      dns_domain = "pr000a.us-west-2.aws.confluent.cloud"

      aws = [{
        vpc_endpoint_service_name = "com.amazonaws.vpce.us-west-2.vpce-svc-00000000000000000"
      }]
    }
  }

  mock_resource "confluent_private_link_attachment_connection" {
    defaults = {
      id = "plattc-000000"
    }
  }

  // The gateway reports the AWS account the ENI permission has to be granted
  // to. It is Confluent's account, not ours, and it differs per gateway.
  mock_resource "confluent_gateway" {
    defaults = {
      id = "gw-000000"

      aws_private_network_interface_gateway = {
        account = "000000000000"
      }
    }
  }

  mock_resource "confluent_access_point" {
    defaults = {
      id = "ap-000000"
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

  landing_topics = {
    dfe_land = {}
    dfe_dlq  = { retention_ms = 604800000 }
  }

  // No module default: the same numbers a customer dial carries, so a
  // contract run proves what render_dial.py actually sends.
  num_partitions   = 12
  log_retention_ms = 259200000

  // 8 MiB is Confluent's own ceiling for a topic's max.message.bytes. The check
  // block in the body warns above it; these runs stay inside it.
  message_max_bytes = 8388608

  // Six distinct interfaces, which is Confluent's floor for an access point.
  // See the note above the mock_provider for why these are supplied here and
  // not created by the module.
  private_network_interface_ids = [
    "eni-00000000000000001",
    "eni-00000000000000002",
    "eni-00000000000000003",
    "eni-00000000000000004",
    "eni-00000000000000005",
    "eni-00000000000000006",
  ]
}

run "confluent_cloud_contract_outputs" {
  command = plan

  module {
    source = "./confluent-cloud"
  }

  assert {
    condition     = can(tostring(output.bootstrap))
    error_message = "bootstrap must be a string"
  }

  // Confluent returns SASL_SSL://host:port, so this is the assertion that
  // proves the scheme is stripped.
  assert {
    condition     = !strcontains(output.bootstrap, "://") && !endswith(output.bootstrap, ",")
    error_message = "bootstrap must be bare host:port pairs, comma separated"
  }

  assert {
    condition     = strcontains(output.bootstrap, ":9092")
    error_message = "bootstrap must be the Kafka endpoint, which Confluent serves on 9092"
  }

  // The correction to the earlier reading: Confluent does NOT do SCRAM on any
  // tier. It is PLAIN over TLS, with the API key as the username.
  assert {
    condition     = output.auth_type == "plain"
    error_message = "auth_type must be plain -- Confluent Cloud has no SCRAM mechanism on any tier"
  }

  assert {
    condition     = output.credential_ref != "" && output.credential_ref != confluent_api_key.dfe.secret
    error_message = "credential_ref must be the API key's ID, never its secret"
  }

  assert {
    condition     = length(output.network_attachment.subnet_ids) == 3
    error_message = "network_attachment.subnet_ids must carry the private subnet per zone the attachment lives in"
  }

  assert {
    condition     = can(tostring(output.network_attachment.security_group_id))
    error_message = "network_attachment.security_group_id must be a string"
  }

  assert {
    condition     = startswith(output.cluster_arn, "crn://")
    error_message = "cluster_arn must be the cluster's Confluent Resource Name"
  }

  assert {
    condition     = output.cluster_id != ""
    error_message = "cluster_id must be the vendor's handle on the cluster"
  }

  assert {
    condition     = output.bootstrap_iam == "" && output.bootstrap_role_arn == ""
    error_message = "a body with no IAM mechanism must return empty for bootstrap_iam and bootstrap_role_arn"
  }
}

// Freight is the default, and its private handshake is a Private Network
// Interface, not PrivateLink: Confluent's brokers attach to ENIs in our VPC.
run "confluent_cloud_freight_private_network_interface" {
  command = plan

  module {
    source = "./confluent-cloud"
  }

  assert {
    condition     = length(confluent_kafka_cluster.this.freight) == 1
    error_message = "freight is the default tier"
  }

  assert {
    condition     = length(confluent_gateway.pni) == 1 && length(confluent_access_point.pni) == 1
    error_message = "Freight's private attachment is a gateway plus an access point"
  }

  // Supplied interfaces are used as they are, and the module builds none of its
  // own. The default is 17 per zone -- 51 across three, the count Confluent
  // documents so the network layer does not cap a scaling operation.
  assert {
    condition     = length(aws_network_interface.pni) == 0 && length(aws_network_interface_permission.pni) == 0
    error_message = "supplied interfaces must be used as they are, with no second set built beside them"
  }

  assert {
    condition     = confluent_access_point.pni[0].aws_private_network_interface[0].account == "000000000000"
    error_message = "the access point must name the account the interfaces live in, read from the caller"
  }

  assert {
    condition     = length(confluent_private_link_attachment.this) == 0
    error_message = "Freight uses no PrivateLink attachment -- that is Enterprise's shape"
  }

  // Pre-created, because dfe-loader treats a missing landing topic as fatal.
  assert {
    condition     = length(confluent_kafka_topic.landing) == 2
    error_message = "every landing topic must be pre-created"
  }
}

// Enterprise is the PrivateLink shape, and its private DNS is ours to run
// because the endpoint service publishes no verified name.
run "confluent_cloud_enterprise_privatelink" {
  command = plan

  module {
    source = "./confluent-cloud"
  }

  variables {
    tier = "enterprise"
  }

  assert {
    condition     = length(confluent_private_link_attachment.this) == 1 && length(confluent_private_link_attachment_connection.this) == 1
    error_message = "Enterprise attaches through a private link attachment and its connection"
  }

  assert {
    condition     = aws_vpc_endpoint.this[0].private_dns_enabled == false
    error_message = "the endpoint service publishes no verified private DNS name, so AWS must not be asked to resolve it"
  }

  assert {
    condition     = length(aws_route53_zone.privatelink) == 1
    error_message = "a private hosted zone must serve the cluster's domain inside the VPC"
  }

  assert {
    condition     = length(aws_network_interface.pni) == 0
    error_message = "Enterprise uses no Private Network Interfaces -- that is Freight's shape"
  }
}

// One principal at the on-prem grant set, and a SEPARATE manager account that
// writes it -- the DFE credential never holds cluster administration.
run "confluent_cloud_acls_at_on_prem_parity" {
  command = plan

  module {
    source = "./confluent-cloud"
  }

  assert {
    condition     = length(confluent_kafka_acl.dfe) == 6
    error_message = "the grant set is four topic operations plus two consumer group operations"
  }

  assert {
    condition     = alltrue([for acl in values(confluent_kafka_acl.dfe) : acl.permission == "ALLOW"])
    error_message = "every grant must be an ALLOW -- a DENY here would be a rule on-prem does not have"
  }

  assert {
    condition     = confluent_role_binding.manager.role_name == "CloudClusterAdmin"
    error_message = "the manager account is what writes the first ACL, and it needs cluster administration to do it"
  }

  assert {
    condition     = confluent_role_binding.manager.principal == "User:${confluent_service_account.manager.id}"
    error_message = "cluster administration must sit on the manager account, never on the credential DFE runs as"
  }

  assert {
    condition     = alltrue([for acl in values(confluent_kafka_acl.dfe) : acl.principal == "User:${confluent_service_account.dfe.id}"])
    error_message = "every grant must name the DFE principal -- Confluent's principal is the service account's id, not its display name"
  }
}

// Freight has no public endpoint and Basic has no private networking. Neither
// refusal is ours -- both are the product's shape, and both fail at plan.
run "confluent_cloud_rejects_public_freight" {
  command = plan

  module {
    source = "./confluent-cloud"
  }

  variables {
    tier         = "freight"
    connectivity = "public"
  }

  expect_failures = [
    var.connectivity,
  ]
}

run "confluent_cloud_rejects_private_basic" {
  command = plan

  module {
    source = "./confluent-cloud"
  }

  variables {
    tier         = "basic"
    connectivity = "private"
  }

  expect_failures = [
    var.connectivity,
  ]
}

// Basic on a public endpoint is the cheapest proof, and it builds no attachment
// at all -- which is also what makes it the wrong shape for a product deploy.
run "confluent_cloud_basic_public_proof" {
  command = plan

  module {
    source = "./confluent-cloud"
  }

  variables {
    tier         = "basic"
    connectivity = "public"
    // Basic reads no data.aws_region -- there is no VPC to discover it from
    // on the public path, so the caller names it directly.
    region = "us-west-2"
  }

  assert {
    condition     = length(confluent_kafka_cluster.this.basic) == 1
    error_message = "the basic tier block must be the one rendered"
  }

  assert {
    condition     = confluent_kafka_cluster.this.region == "us-west-2"
    error_message = "region must resolve from var.region on the public path, where there is no VPC to discover it from"
  }

  assert {
    condition     = length(aws_security_group.private) == 0
    error_message = "a public cluster builds no private attachment"
  }

  assert {
    condition     = output.auth_type == "plain"
    error_message = "auth_type is plain on every Confluent tier"
  }
}
