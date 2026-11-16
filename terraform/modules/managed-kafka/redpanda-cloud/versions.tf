// redpanda creates the cluster and its endpoint service, aws the endpoint that connects to it. The calling root configures both providers.

terraform {
  // 1.11 is the floor for write-only arguments.
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
