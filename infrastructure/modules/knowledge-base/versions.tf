terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    # This module creates a null_resource to bootstrap the AOSS index. Declare
    # the provider explicitly rather than relying on Terraform auto-installing
    # the legacy "null" provider by name.
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
  }
}
