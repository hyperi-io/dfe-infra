// Redpanda Cloud Serverless over AWS PrivateLink. It is the only tier this organisation can create, and variables.tf refuses the others.
// Redpanda runs the brokers, so nothing here sizes or tunes them. docs/deployment/kafka/redpanda-cloud.md#module-design-notes.

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_region" "current" {}

locals {
  private = var.connectivity == "private"

  // Serverless takes the cloud's own region token, here the aws provider's region, and offers private connectivity on AWS alone.
  region = data.aws_region.current.region

  network_state = {
    enabled  = "STATE_ENABLED"
    disabled = "STATE_DISABLED"
  }

  // Private connectivity reaches the data plane through the endpoint alone, so tofu must run inside the VPC or behind a Route 53 Resolver forwarding rule.
  cluster_api_url = local.private ? redpanda_serverless_cluster.this.dataplane_api.private_url : redpanda_serverless_cluster.this.dataplane_api.url

  seed_brokers = local.private ? redpanda_serverless_cluster.this.kafka_api.private_seed_brokers : redpanda_serverless_cluster.this.kafka_api.seed_brokers

  // Redpanda returns a list with a scheme, and the contract wants bare host:port.
  bootstrap = join(",", [for broker in local.seed_brokers : replace(broker, "/^[a-zA-Z0-9+.-]+:\\/\\//", "")])
}

resource "redpanda_resource_group" "this" {
  count = var.resource_group_id == "" ? 1 : 0

  name = var.name
}

// Keeps an existing deployment's group from planning as a replacement.
moved {
  from = redpanda_resource_group.this
  to   = redpanda_resource_group.this[0]
}

locals {
  resource_group_id = var.resource_group_id != "" ? var.resource_group_id : redpanda_resource_group.this[0].id
}

resource "redpanda_serverless_cluster" "this" {
  name              = var.name
  resource_group_id = local.resource_group_id
  serverless_region = local.region

  // Without private_link_id the private half of networking_config has nothing to attach to.
  private_link_id = local.private ? redpanda_serverless_private_link.this[0].id : null

  networking_config = {
    private = local.private ? local.network_state.enabled : local.network_state.disabled
    public  = local.private ? local.network_state.disabled : local.network_state.enabled
  }

  allow_deletion = var.allow_deletion

  tags = {
    env          = var.env
    deployment   = var.name
    "managed-by" = "opentofu"
  }
}

// Allows this account, read from the caller, to connect to Redpanda's endpoint service.
resource "redpanda_serverless_private_link" "this" {
  count = local.private ? 1 : 0

  name              = "${var.name}-private-link"
  resource_group_id = local.resource_group_id
  serverless_region = local.region

  // Redpanda's token for the deployment's cloud. var.cloud refuses all but aws.
  cloud_provider = var.cloud

  aws_config = {
    allowed_principals = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
  }

  allow_deletion = var.allow_deletion
}
