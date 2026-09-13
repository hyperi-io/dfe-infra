// Redpanda Cloud Serverless, reached over AWS PrivateLink. Serverless because
// it is the only tier this organisation can create -- Dedicated and BYOC are
// Request-access and quote-only, and variables.tf refuses them at plan.
//
// The vendor runs the brokers, so nothing here sizes, tunes or versions them:
// the canonical broker profile applies to the Kafka we run, and what survives
// on this path is the per-topic half.

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_region" "current" {}

locals {
  private = var.connectivity == "private"

  // Redpanda names a serverless region with the cloud provider's own region
  // token, so the region is whichever one the root pointed the aws provider at.
  // That is also the only cloud where Serverless offers private connectivity.
  region = data.aws_region.current.region

  // Redpanda's own state words for the two halves of networking_config.
  network_state = {
    enabled  = "STATE_ENABLED"
    disabled = "STATE_DISABLED"
  }

  // On private connectivity the data plane is reachable only through the
  // endpoint, so the topics, the user and the ACLs are written against the
  // private URL -- which means tofu itself has to run inside the VPC, or behind
  // a Route 53 Resolver rule that forwards the cluster domain into it.
  cluster_api_url = local.private ? redpanda_serverless_cluster.this.dataplane_api.private_url : redpanda_serverless_cluster.this.dataplane_api.url

  seed_brokers = local.private ? redpanda_serverless_cluster.this.kafka_api.private_seed_brokers : redpanda_serverless_cluster.this.kafka_api.seed_brokers

  // The contract wants bare host:port pairs. Redpanda returns a list, and the
  // scheme is stripped rather than assumed absent.
  bootstrap = join(",", [for broker in local.seed_brokers : replace(broker, "/^[a-zA-Z0-9+.-]+:\\/\\//", "")])
}

// ---------------------------------------------------------------------------
// The cluster
// ---------------------------------------------------------------------------

resource "redpanda_resource_group" "this" {
  name = var.name
}

resource "redpanda_serverless_cluster" "this" {
  name              = var.name
  resource_group_id = redpanda_resource_group.this.id
  serverless_region = local.region

  // Set private_link_id and the cluster answers on the endpoint; leave it null
  // and the private half of networking_config has nothing to attach to.
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

// ---------------------------------------------------------------------------
// The private handshake: Redpanda's end
// ---------------------------------------------------------------------------

// Redpanda publishes an AWS VPC endpoint SERVICE and allows this account to
// connect to it. The account is read from the caller rather than written down,
// because the deployment account is deployment config.
resource "redpanda_serverless_private_link" "this" {
  count = local.private ? 1 : 0

  name              = "${var.name}-private-link"
  resource_group_id = redpanda_resource_group.this.id
  serverless_region = local.region

  // The vendor's own token for the cloud this body's second provider talks to.
  cloud_provider = "aws"

  aws_config = {
    allowed_principals = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
  }

  allow_deletion = var.allow_deletion
}
