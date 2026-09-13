terraform {
  // >= 1.11 to match the aws root this module is always called from
  // (terraform/environments/aws/versions.tf) -- no feature of its own needs
  // a newer floor.
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.64"
    }
  }
}
