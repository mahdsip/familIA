#!/usr/bin/env bash
# Creates the S3 bucket used for Terraform remote state (one-time bootstrap).
# The bucket is private, versioned and encrypted. State locking uses S3's
# native lockfile (use_lockfile=true) so no DynamoDB table is needed.
#
# Usage:
#   AWS_PROFILE=<your-profile> ./scripts/bootstrap_backend.sh <bucket-name> [region]
set -euo pipefail

BUCKET="${1:?Usage: bootstrap_backend.sh <bucket-name> [region]}"
REGION="${2:-eu-central-1}"

echo "Creating state bucket s3://${BUCKET} in ${REGION}..."

if [ "${REGION}" = "us-east-1" ]; then
  aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}"
else
  aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" \
    --create-bucket-configuration LocationConstraint="${REGION}"
fi

aws s3api put-public-access-block --bucket "${BUCKET}" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-versioning --bucket "${BUCKET}" \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption --bucket "${BUCKET}" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"},"BucketKeyEnabled":true}]}'

cat <<EOF

Done. Initialise Terraform with:

  cd terraform
  terraform init \\
    -backend-config="bucket=${BUCKET}" \\
    -backend-config="key=familia/terraform.tfstate" \\
    -backend-config="region=${REGION}" \\
    -backend-config="use_lockfile=true"
EOF
