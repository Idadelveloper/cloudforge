terraform {
  required_version = ">= 1.3.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }

    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }

    local = {
      source  = "hashicorp/local"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region

  # LocalStack friendly settings - these are no-ops against real AWS credentials.
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_region_validation      = true

  default_tags {
    tags = {
      project      = var.project_name
      "managed-by" = "terraform"
    }
  }
}
