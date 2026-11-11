// ONE provider for the deployment. The modules declare what they need and this
// block configures it, so a tag policy or a region change lands in one place.
// local.tags is the governance set plus a test run's id and expiry, so a
// guardrail that refuses an untagged create sees both on the create call.
provider "aws" {
  region = var.provision.region

  default_tags {
    tags = local.tags
  }
}

locals {
  // The provider's own floor for its IAM, API key and RBAC clients. At the default 4, about 15s of connection resets from the cluster's REST endpoint fail the manager key's readiness wait, and the apply with it.
  confluent_max_retries = 12
}

// Confluent Cloud's own org-level management credential. Never a tfvar: the
// deployer's shell carries CONFLUENT_CLOUD_API_KEY and CONFLUENT_CLOUD_API_SECRET,
// which the provider reads on its own the same way this root leans on the aws
// provider's own AWS_* environment resolution rather than a credential variable.
provider "confluent" {
  max_retries = local.confluent_max_retries
}

// Redpanda Cloud's own organisation IAM client. Never a tfvar, for the same
// reason: the deployer's shell carries REDPANDA_CLIENT_ID and
// REDPANDA_CLIENT_SECRET.
provider "redpanda" {}
