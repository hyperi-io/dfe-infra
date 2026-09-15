// Two providers, because the private handshake has two ends: redpanda creates
// the cluster and publishes a VPC endpoint SERVICE, and aws creates the VPC
// endpoint that connects to it. Neither provider can do the other's half.
//
// The provider itself -- credentials, region, default tags -- is configured by
// the root that calls this module.

terraform {
  // 1.11 is the floor for write-only arguments, which is how the SCRAM password
  // reaches Redpanda without being written to state.
  required_version = ">= 1.11"

  required_providers {
    redpanda = {
      source  = "redpanda-data/redpanda"
      version = "~> 2.3"
    }

    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.64"
    }
  }
}
