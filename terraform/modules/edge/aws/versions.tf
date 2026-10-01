// A module declares what it needs and nothing more. The provider itself --
// region, credentials, default tags -- is configured by the root that calls it,
// so that one deployment has one provider however many modules it composes.

terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.64"
    }
  }
}
