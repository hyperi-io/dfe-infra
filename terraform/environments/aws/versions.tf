terraform {
  // The highest module floor: kubernetes-cluster/aws and s3-bucket need 1.12.
  required_version = ">= 1.12"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.64"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    // Used only when kafka.provider selects the matching SaaS body.
    confluent = {
      source  = "confluentinc/confluent"
      version = "~> 2.86"
    }
    redpanda = {
      source  = "redpanda-data/redpanda"
      version = "~> 2.3"
    }
  }

  // terraform/environments/aws-state creates the bucket.
  backend "s3" {
    bucket       = var.state.bucket
    key          = var.state.key
    region       = var.state.region
    encrypt      = true
    use_lockfile = true
  }
}
