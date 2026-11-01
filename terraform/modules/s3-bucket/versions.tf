// A module declares what it needs; the root configures the provider.

terraform {
  // 1.12 for lifecycle.destroy, which keeps a retained bucket and its configuration.
  required_version = ">= 1.12"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.64"
    }
  }
}
