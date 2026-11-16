// The deployment's one aws provider. default_tags puts local.tags on the create call, which an untagged-create guardrail checks.
provider "aws" {
  region = var.provision.region

  default_tags {
    tags = local.tags
  }
}

locals {
  // At the provider's default of 4 retries, about 15s of connection resets from the cluster's REST endpoint fail the manager key's readiness wait.
  confluent_max_retries = 12
}

// The credential is never a tfvar: the provider reads CONFLUENT_CLOUD_API_KEY and CONFLUENT_CLOUD_API_SECRET from the environment.
provider "confluent" {
  max_retries = local.confluent_max_retries
}

// Reads REDPANDA_CLIENT_ID and REDPANDA_CLIENT_SECRET from the environment, and fails at configure time without them.
provider "redpanda" {}
