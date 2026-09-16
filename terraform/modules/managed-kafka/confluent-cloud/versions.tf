// Two providers, because the private handshake has two ends: confluent creates
// the cluster and one half of the attachment, and aws creates the endpoint or
// the network interfaces it binds to. Neither provider can do the other's half.
//
// The provider itself -- credentials, region, default tags -- is configured by
// the root that calls this module.

terraform {
  required_version = ">= 1.10"

  required_providers {
    confluent = {
      source  = "confluentinc/confluent"
      version = "~> 2.86"
    }

    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.64"
    }
  }
}
