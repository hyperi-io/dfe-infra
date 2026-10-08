terraform {
  // >= 1.10 for two things this root depends on: variables inside the backend
  // block (early variable evaluation), and native S3 locking, which is what
  // removes the DynamoDB lock table a customer would otherwise have to create.
  //
  // 1.11 is redpanda-cloud's own floor (write-only arguments for the SCRAM
  // password), which this root inherits the moment kafka.provider can select
  // it -- so the constraint states 1.11 rather than the 1.10 this root alone
  // would need.
  required_version = ">= 1.11"

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

// Confluent Cloud's own org-level management credential. Never a tfvar: the
// deployer's shell carries CONFLUENT_CLOUD_API_KEY and CONFLUENT_CLOUD_API_SECRET,
// which the provider reads on its own the same way this root leans on the aws
// provider's own AWS_* environment resolution rather than a credential variable.
provider "confluent" {}

// Redpanda Cloud's own organisation IAM client. Never a tfvar, for the same
// reason: the deployer's shell carries REDPANDA_CLIENT_ID and
// REDPANDA_CLIENT_SECRET.
provider "redpanda" {}
