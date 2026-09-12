terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.64"
    }
  }

  # Local state deliberately: this environment is created and destroyed within a
  # session, so there is no long-lived state worth an S3 backend of its own.
  backend "local" {
    path = "terraform.tfstate"
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "dfe"
      Env       = "spike"
      ManagedBy = "opentofu"
      # Carried by every resource so a sweep can find strays by tag alone if
      # state is ever lost mid-run.
      Spike = "aws-eks"
    }
  }
}
