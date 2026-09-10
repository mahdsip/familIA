#!/usr/bin/env bash
# Deletes the ORIGINAL proof-of-concept resources (created by hand, not by this
# Terraform). Run this ONLY after the Terraform stack is deployed and verified.
#
# This is DESTRUCTIVE. It requires typing DELETE to proceed. It intentionally
# does NOT delete the old documents bucket contents unless you pass --with-bucket
# (your files live there). Review every id before running.
#
# Usage:
#   AWS_PROFILE=<profile> ./scripts/destroy_poc.sh [--with-bucket]
set -euo pipefail

REGION="eu-central-1"
WITH_BUCKET="no"
[ "${1:-}" = "--with-bucket" ] && WITH_BUCKET="yes"

# The old PoC bucket name is provided via env var so no personal handle is
# hard-coded in this public repo. Set it before running, e.g.:
#   POC_BUCKET_NAME=family-docs-xxxxx ./scripts/destroy_poc.sh
POC_BUCKET="${POC_BUCKET_NAME:?Set POC_BUCKET_NAME to the old PoC documents bucket name}"

# --- Old PoC resource identifiers (verified via inventory) -------------------
POC_AGENT_ID="GVCPY7SISO"              # family-agent-family-assistant
POC_AGENT_QUICKSTART="IQCJT5KULZ"      # agent-quick-start-15u1k
POC_KB_ID="IEI7CAWWBB"                 # knowledge-base-familIA
POC_API_ID="onp96dxjt1"                # family-agent-api (HTTP API)
POC_LAMBDAS=(family-agent-api family-agent-kb-provisioner family-agent-auto-sync)
POC_LOG_GROUPS=(
  /aws/lambda/family-agent-api
  /aws/lambda/family-agent-kb-provisioner
  /aws/lambda/family-agent-auto-sync
  /aws/apigateway/family-agent
)

echo "About to DELETE the following PoC resources in ${REGION}:"
echo "  Bedrock agents:    ${POC_AGENT_ID}, ${POC_AGENT_QUICKSTART}"
echo "  Bedrock KB:        ${POC_KB_ID}"
echo "  HTTP API:          ${POC_API_ID}"
echo "  Lambda functions:  ${POC_LAMBDAS[*]}"
echo "  Log groups:        ${POC_LOG_GROUPS[*]}"
if [ "${WITH_BUCKET}" = "yes" ]; then
  echo "  S3 bucket + CONTENTS: ${POC_BUCKET}  <-- includes your documents!"
else
  echo "  S3 bucket ${POC_BUCKET}: KEPT (pass --with-bucket to delete it)"
fi
echo
read -r -p "Type DELETE to proceed: " CONFIRM
[ "${CONFIRM}" = "DELETE" ] || { echo "Aborted."; exit 1; }

echo "Deleting Bedrock agents..."
aws bedrock-agent delete-agent --region "${REGION}" --agent-id "${POC_AGENT_ID}" --skip-resource-in-use-check || true
aws bedrock-agent delete-agent --region "${REGION}" --agent-id "${POC_AGENT_QUICKSTART}" --skip-resource-in-use-check || true

echo "Deleting Bedrock knowledge base (and its data sources)..."
for ds in $(aws bedrock-agent list-data-sources --region "${REGION}" --knowledge-base-id "${POC_KB_ID}" \
              --query "dataSourceSummaries[].dataSourceId" --output text 2>/dev/null || true); do
  aws bedrock-agent delete-data-source --region "${REGION}" --knowledge-base-id "${POC_KB_ID}" --data-source-id "${ds}" || true
done
aws bedrock-agent delete-knowledge-base --region "${REGION}" --knowledge-base-id "${POC_KB_ID}" || true

echo "Deleting HTTP API..."
aws apigatewayv2 delete-api --region "${REGION}" --api-id "${POC_API_ID}" || true

echo "Deleting Lambda functions..."
for fn in "${POC_LAMBDAS[@]}"; do
  aws lambda delete-function --region "${REGION}" --function-name "${fn}" || true
done

echo "Deleting log groups..."
for lg in "${POC_LOG_GROUPS[@]}"; do
  aws logs delete-log-group --region "${REGION}" --log-group-name "${lg}" || true
done

if [ "${WITH_BUCKET}" = "yes" ]; then
  echo "Emptying and deleting bucket ${POC_BUCKET}..."
  aws s3 rm "s3://${POC_BUCKET}" --recursive || true
  aws s3api delete-bucket --bucket "${POC_BUCKET}" --region "${REGION}" || true
fi

echo "PoC cleanup complete."
