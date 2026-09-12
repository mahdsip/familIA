#!/usr/bin/env bash
# Runs Bedrock Knowledge Base ingestion to completion, in batches.
#
# When advanced (foundation-model) parsing is enabled, Bedrock indexes at most
# 1000 files per ingestion job. If you have more documents than that, a single
# job leaves the rest unprocessed. This script starts ingestion jobs in a loop,
# each picking up where the last left off, until a run indexes 0 new documents
# (i.e. everything is done) or the safety cap of MAX_RUNS is reached.
#
# Credentials: standard AWS chain (env vars, AWS_PROFILE, or SSO).
#
# Usage:
#   AWS_PROFILE=<profile> ./ingest.sh <knowledge-base-id> <data-source-id> [region]
#
# Tip: get the ids from Terraform:
#   KB=$(cd terraform && terraform output -raw knowledge_base_id)
#   DS=$(cd terraform && terraform output -raw data_source_id)
#   ./scripts/ingest.sh "$KB" "$DS"
set -euo pipefail

KB="${1:?Usage: ingest.sh <knowledge-base-id> <data-source-id> [region]}"
DS="${2:?Missing data-source-id}"
REGION="${3:-eu-central-1}"
MAX_RUNS="${MAX_RUNS:-10}"

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "ERROR: no valid AWS credentials found (env vars / AWS_PROFILE / SSO)." >&2
  exit 1
fi

run=1
while [ "${run}" -le "${MAX_RUNS}" ]; do
  echo "=================================================================="
  echo "Ingestion run ${run}/${MAX_RUNS}"

  JOB=$(aws bedrock-agent start-ingestion-job \
    --knowledge-base-id "${KB}" --data-source-id "${DS}" --region "${REGION}" \
    --description "Batched ingestion run ${run}" \
    --query "ingestionJob.ingestionJobId" --output text)
  echo "  job: ${JOB}"

  # Poll until the job finishes.
  while true; do
    sleep 30
    STATUS=$(aws bedrock-agent get-ingestion-job \
      --knowledge-base-id "${KB}" --data-source-id "${DS}" \
      --ingestion-job-id "${JOB}" --region "${REGION}" \
      --query "ingestionJob.status" --output text)
    echo "  $(date +%H:%M:%S) status=${STATUS}"
    case "${STATUS}" in
      COMPLETE|FAILED) break ;;
    esac
  done

  # Read this run's stats.
  read -r INDEXED FAILED SCANNED <<EOF
$(aws bedrock-agent get-ingestion-job \
    --knowledge-base-id "${KB}" --data-source-id "${DS}" \
    --ingestion-job-id "${JOB}" --region "${REGION}" \
    --query "ingestionJob.statistics.[numberOfNewDocumentsIndexed,numberOfDocumentsFailed,numberOfDocumentsScanned]" \
    --output text)
EOF
  echo "  indexed=${INDEXED} failed=${FAILED} scanned=${SCANNED} (status=${STATUS})"

  # Stop when a run indexes no new documents — nothing left to do.
  if [ "${INDEXED:-0}" -eq 0 ]; then
    echo "=================================================================="
    echo "No new documents indexed this run — ingestion complete."
    break
  fi

  run=$((run + 1))
done

echo "Done. Note: 'failed' counts include unsupported media/app files that are"
echo "intentionally not indexed; check failureReasons if the number is unexpected."
