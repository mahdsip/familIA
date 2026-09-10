#!/usr/bin/env bash
# Syncs your local documents folder to the familIA S3 bucket AND keeps the
# folder-derived metadata in sync.
#
# Designed to run from the machine that HOLDS your documents (not necessarily
# the one with the Terraform repo). It only needs:
#   - Python 3
#   - AWS CLI v2, configured with credentials that can write the bucket
#   - this script + generate_metadata.py + familia.config.json in the same dir
#
# What it does, every run:
#   1. Regenerates the <file>.metadata.json sidecars from the CURRENT folder
#      structure. If you reorganised folders, the metadata updates to match.
#   2. Uploads documents + their sidecars to S3 (mirror with --delete).
#   3. Reports whether anything changed. Uploading objects triggers the
#      auto-sync Lambda, which reindexes the Knowledge Base.
#
# Usage:
#   AWS_PROFILE=<profile> ./sync_docs.sh <local-docs-dir> <bucket-name> [prefix]
#
# Example weekly cron (Sundays 02:30, before the KB safety-net at 03:00):
#   30 2 * * 0  AWS_PROFILE=familia /path/to/sync_docs.sh "$HOME/Documents/Family" familia-docs-123456789012 documents
set -euo pipefail

LOCAL_DIR="${1:?Usage: sync_docs.sh <local-docs-dir> <bucket-name> [prefix]}"
BUCKET="${2:?Missing bucket name}"
PREFIX="${3:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

DEST="s3://${BUCKET}"
[ -n "${PREFIX}" ] && DEST="${DEST}/${PREFIX}"

# 1. Regenerate metadata sidecars from the current folder structure. Capture
#    output so we can tell whether the structure/metadata changed.
echo "==> Updating metadata from folder structure..."
META_OUT="$(python3 "${SCRIPT_DIR}/generate_metadata.py" "${LOCAL_DIR}" --prune)"
echo "    ${META_OUT}"

# 2. Sync documents + sidecars to S3 (mirror). --delete removes objects that no
#    longer exist locally (including sidecars pruned in step 1).
echo "==> Syncing ${LOCAL_DIR} -> ${DEST}"
SYNC_OUT="$(aws s3 sync "${LOCAL_DIR}" "${DEST}" \
  --delete \
  --exclude ".DS_Store" \
  --exclude "*/.DS_Store" \
  --exclude "Thumbs.db" \
  --sse aws:kms)"

if [ -n "${SYNC_OUT}" ]; then
  echo "${SYNC_OUT}"
  echo "==> Upload changes detected. The auto-sync Lambda will reindex the knowledge base."
else
  echo "==> Nothing to upload; S3 already matches your local folder."
fi
