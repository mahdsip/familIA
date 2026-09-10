#!/usr/bin/env bash
# Syncs one or more local document roots to the familIA S3 bucket AND keeps the
# folder-derived metadata in sync.
#
# Designed to run from the machine that HOLDS your documents (not necessarily
# the one with the Terraform repo). It only needs:
#   - Python 3
#   - AWS CLI v2, configured with credentials that can write the bucket
#   - this script + generate_metadata.py + familia.config.json in the same dir
#
# What it does, for EACH root, every run:
#   1. Regenerates the <file>.metadata.json sidecars from the CURRENT folder
#      structure (recursively, all subfolders). If you reorganised folders, the
#      metadata updates to match.
#   2. Uploads documents + their sidecars to S3 (mirror with --delete).
#   3. Reports whether anything changed. Uploading objects triggers the
#      auto-sync Lambda, which reindexes the Knowledge Base.
#
# MULTIPLE ROOTS: each root is mirrored into its OWN S3 subprefix, named after
# the root's folder (its basename), so roots never delete each other's objects
# under --delete. Final S3 layout:
#   s3://<bucket>/<prefix>/<root-basename>/<your folder tree...>
# Override a root's subprefix by passing "path=subname" instead of just "path".
#
# Usage:
#   AWS_PROFILE=<profile> ./sync_docs.sh <bucket-name> <prefix> <root> [<root> ...]
#
#   <prefix> may be empty ("") to sync at the bucket root.
#   Each <root> is a local directory; "<root>=<subname>" sets its S3 subprefix.
#
# Examples:
#   ./sync_docs.sh familia-docs-123 documents "$HOME/Docs/Family"
#   ./sync_docs.sh familia-docs-123 documents "$HOME/Docs/Family" "$HOME/Scans"
#   ./sync_docs.sh familia-docs-123 documents "/mnt/nas/health=salud" "/mnt/nas/school=colegio"
#
# Weekly cron (Sundays 02:30, before the KB safety-net at 03:00):
#   30 2 * * 0  AWS_PROFILE=familia /path/to/sync_docs.sh familia-docs-123 documents "$HOME/Docs/Family"
set -euo pipefail

BUCKET="${1:?Usage: sync_docs.sh <bucket-name> <prefix> <root> [<root> ...]}"
PREFIX="${2?Missing prefix (use \"\" for bucket root)}"
shift 2
[ "$#" -ge 1 ] || { echo "Provide at least one document root." >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

any_changes=0

for ARG in "$@"; do
  # Split optional "path=subname".
  if [[ "${ARG}" == *"="* ]]; then
    ROOT="${ARG%%=*}"
    SUB="${ARG#*=}"
  else
    ROOT="${ARG}"
    SUB="$(basename "${ROOT}")"
  fi

  if [ ! -d "${ROOT}" ]; then
    echo "!! Skipping '${ROOT}': not a directory" >&2
    continue
  fi

  # Build the destination: <bucket>[/<prefix>]/<sub>
  DEST="s3://${BUCKET}"
  [ -n "${PREFIX}" ] && DEST="${DEST}/${PREFIX}"
  DEST="${DEST}/${SUB}"

  echo "==================================================================="
  echo "Root: ${ROOT}"
  echo "  ->  ${DEST}"

  # 1. Regenerate metadata sidecars for this root (recursive, --prune orphans).
  echo "==> Updating metadata from folder structure..."
  META_OUT="$(python3 "${SCRIPT_DIR}/generate_metadata.py" "${ROOT}" --prune)"
  echo "    ${META_OUT}"

  # 2. Mirror documents + sidecars to this root's subprefix. --delete only
  #    affects THIS subprefix, so roots don't clobber each other.
  echo "==> Syncing..."
  SYNC_OUT="$(aws s3 sync "${ROOT}" "${DEST}" \
    --delete \
    --exclude ".DS_Store" \
    --exclude "*/.DS_Store" \
    --exclude "Thumbs.db" \
    --sse aws:kms)"

  if [ -n "${SYNC_OUT}" ]; then
    echo "${SYNC_OUT}"
    any_changes=1
  else
    echo "    (nothing to upload; S3 already matches this root)"
  fi
done

echo "==================================================================="
if [ "${any_changes}" -eq 1 ]; then
  echo "==> Upload changes detected. The auto-sync Lambda will reindex the knowledge base."
else
  echo "==> No changes across any root; knowledge base is already up to date."
fi
