// The calling root configures the provider.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.64"
    }

    // Zips broker_scaler.py for the autoscaler Lambda.
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }
  }
}
