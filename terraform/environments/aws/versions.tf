terraform {
  // >= 1.10 for two things this root depends on: variables inside the backend
  // block (early variable evaluation), and native S3 locking, which is what
  // removes the DynamoDB lock table a customer would otherwise have to create.
  //
  // 1.11 is redpanda-cloud's own floor (write-only arguments for the SCRAM
  // password), and 1.12 is kubernetes-cluster/aws's (lifecycle.destroy on the
  // deployment key). This root inherits both, so the constraint states 1.12.
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
    // Only configured when kafka.provider selects the matching SaaS body
    // (managed-kafka/confluent-cloud, managed-kafka/redpanda-cloud); a provider
    // block with no resource against it costs nothing at plan or apply.
    confluent = {
      source  = "confluentinc/confluent"
      version = "~> 2.86"
    }
    redpanda = {
      source  = "redpanda-data/redpanda"
      version = "~> 2.3"
    }
  }

  // Every field is a variable, so the state location is deployment config like
  // everything else. terraform/environments/aws-state creates the bucket.
  backend "s3" {
    bucket       = var.state.bucket
    key          = var.state.key
    region       = var.state.region
    encrypt      = true
    use_lockfile = true
  }
}
