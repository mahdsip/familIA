# ---------------------------------------------------------------------------
# Terraform & provider version constraints
#
# S3 Vectors resources (aws_s3vectors_vector_bucket / aws_s3vectors_index) and
# the S3_VECTORS storage type for Bedrock Knowledge Bases require a recent AWS
# provider. We pin to the 6.x line which ships these resources.
# ---------------------------------------------------------------------------
terraform {
  # >= 1.10 enables native S3 state locking (use_lockfile) — no DynamoDB table.
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }

  # -------------------------------------------------------------------------
  # Remote state backend.
  #
  # State can contain sensitive values, so it must NOT live in the public repo.
  # This backend is intentionally left as a partial configuration: the bucket
  # name is supplied at init time so it never gets hard-coded here. Bootstrap
  # the backend bucket once (see scripts/bootstrap_backend.sh), then run:
  #
  #   terraform init \
  #     -backend-config="bucket=<your-tfstate-bucket>" \
  #     -backend-config="key=familia/terraform.tfstate" \
  #     -backend-config="region=eu-central-1" \
  #     -backend-config="use_lockfile=true"
  #
  # To use local state instead (simpler, but never commit the .tfstate file),
  # comment out this whole block.
  # -------------------------------------------------------------------------
  backend "s3" {}
}
