# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------
provider "aws" {
  region = var.aws_region

  # Tags applied to every taggable resource, so the whole stack is easy to
  # find, audit and (if ever needed) clean up.
  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "Terraform"
      Repository  = "familIA"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
