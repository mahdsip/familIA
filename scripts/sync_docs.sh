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
# CREDENTIALS: resolved via the standard AWS chain, so any of these work with
# no change — you do NOT have to use AWS_PROFILE:
#   - Exported env vars: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
#     (+ AWS_SESSION_TOKEN for temporary creds)
#   - A named profile: AWS_PROFILE=<name>
#   - SSO / instance / container roles
# Region comes from AWS_REGION / AWS_DEFAULT_REGION, the profile, or the
# bucket's own region.
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
# Usage (credentials via env vars, a profile, or SSO — see CREDENTIALS above):
#   ./sync_docs.sh <bucket-name> <prefix> <root> [<root> ...]
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

# DRY_RUN=1 verifies what WOULD happen without changing anything: metadata is
# not written and no objects are uploaded or deleted. Use it to test the script
# safely, e.g.:  DRY_RUN=1 ./sync_docs.sh <bucket> documents "<root>"
DRY_RUN="${DRY_RUN:-0}"
META_FLAGS=(--prune)
SYNC_FLAGS=()
if [ "${DRY_RUN}" != "0" ]; then
  echo "*** DRY RUN — no metadata written, no uploads/deletes performed ***"
  META_FLAGS+=(--dry-run)
  SYNC_FLAGS+=(--dryrun)
fi

# Only these file types are useful to a text/RAG knowledge base. We upload an
# ALLOWLIST (exclude everything, then re-include these), which keeps out media
# (mp4/heic), medical imaging (dcm) and bundled app internals (dll/jar/exe/nib)
# that Bedrock can't parse and that waste the 1000-file/advanced-parsing budget.
# Extend DOC_EXTS if you need more document formats.
DOC_EXTS="pdf txt md csv doc docx xls xlsx ppt pptx html htm json rtf odt"

# Build the --include flags: the .metadata.json sidecars plus each doc type
# (both lower- and upper-case extensions).
INCLUDES=(--include "*.metadata.json")
for e in ${DOC_EXTS}; do
  E_UPPER="$(printf '%s' "$e" | tr '[:lower:]' '[:upper:]')"
  INCLUDES+=(--include "*.${e}" --include "*.${E_UPPER}")
done

# Fail fast if no usable AWS credentials resolve from the standard chain
# (exported env vars, AWS_PROFILE, SSO, instance role, ...). This avoids
# failing halfway through an upload.
if ! CALLER="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null)"; then
  echo "ERROR: no valid AWS credentials found." >&2
  echo "Provide credentials via exported env vars (AWS_ACCESS_KEY_ID/" >&2
  echo "AWS_SECRET_ACCESS_KEY[/AWS_SESSION_TOKEN]), a profile (AWS_PROFILE=<name>)," >&2
  echo "or an SSO login, then re-run." >&2
  exit 1
fi
echo "==> Using AWS identity: ${CALLER}"

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
  META_OUT="$(python3 "${SCRIPT_DIR}/generate_metadata.py" "${ROOT}" "${META_FLAGS[@]}")"
  echo "    ${META_OUT}"

  # 2. Mirror documents + sidecars to this root's subprefix. Allowlist: exclude
  #    everything, then re-include only document types + sidecars. --delete also
  #    removes any previously-uploaded junk (media/app files) from S3 for this
  #    subprefix, so re-running cleans up past over-uploads.
  echo "==> Syncing (documents + sidecars only)..."
  # Note the ${arr[@]+"${arr[@]}"} idiom: safe expansion of a possibly-empty
  # array under `set -u` on bash 3.2 (macOS default).
  SYNC_OUT="$(aws s3 sync "${ROOT}" "${DEST}" \
    --delete \
    --exclude "*" \
    "${INCLUDES[@]}" \
    ${SYNC_FLAGS[@]+"${SYNC_FLAGS[@]}"} \
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
