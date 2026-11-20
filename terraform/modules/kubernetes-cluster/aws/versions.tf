// The root configures the provider, so one deployment has one provider however many modules it
// composes.

terraform {
  // 1.12 for lifecycle.destroy, which keeps a persistent deployment's key (kms.tf).
  required_version = ">= 1.12"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.64"
    }
  }
}
