terraform {
  required_version = ">= 1.3.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
  }
}

provider "aws" {
  region = var.aws_region

  # LocalStack serves S3 over path-style addressing.
  s3_use_path_style = var.s3_use_path_style

  default_tags {
    tags = {
      project      = var.project_name
      "managed-by" = "terraform"
    }
  }
}
