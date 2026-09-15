terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.64"
    }
  }

  // LOCAL state, deliberately and only here. This root creates the bucket every
  // other root keeps its state in, so it cannot keep its own state in it. It is
  // run once per account and creates one bucket; losing its state costs an
  // import, not a rebuild.
  backend "local" {
    path = "terraform.tfstate"
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = var.tags
  }
}
