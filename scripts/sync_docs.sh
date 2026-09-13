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

# Allowlist of file types to upload. We exclude everything, then re-include
# these — keeping out media (mp4/heic), medical imaging (dcm) and bundled app
# internals (dll/jar/exe/nib) that Bedrock can't parse and that waste the
# 1000-file/advanced-parsing budget.
#
# The list is loaded from options.allowed_extensions in familia.config.json so
# you manage formats in one place. Falls back to this built-in default if the
# config file or the key is missing. Override ad hoc with the DOC_EXTS env var.
DEFAULT_DOC_EXTS="pdf txt md csv doc docx xls xlsx ppt pptx html htm json rtf odt jpg jpeg png"
CONFIG_FILE="${FAMILIA_CONFIG:-${SCRIPT_DIR}/familia.config.json}"

if [ -n "${DOC_EXTS:-}" ]; then
  : # explicit env override wins, use as-is
elif [ -f "${CONFIG_FILE}" ]; then
  DOC_EXTS="$(python3 - "${CONFIG_FILE}" <<'PY'
import json, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    exts = (cfg.get("options", {}) or {}).get("allowed_extensions") or []
    # normalise: strip leading dots, lowercase, drop blanks
    exts = [str(e).lstrip(".").strip().lower() for e in exts if str(e).strip()]
    print(" ".join(dict.fromkeys(exts)))  # de-dupe, preserve order
except Exception:
    print("")
PY
)"
  # Fall back to default if the config had no usable list.
  [ -n "${DOC_EXTS}" ] || DOC_EXTS="${DEFAULT_DOC_EXTS}"
  echo "==> Allowed extensions from ${CONFIG_FILE}: ${DOC_EXTS}"
else
  DOC_EXTS="${DEFAULT_DOC_EXTS}"
fi

# Build the --include flags: the .metadata.json sidecars plus each doc type
# (both lower- and upper-case extensions).
INCLUDES=(--include "*.metadata.json")
for e in ${DOC_EXTS}; do
  E_UPPER="$(printf '%s' "$e" | tr '[:lower:]' '[:upper:]')"
  INCLUDES+=(--include "*.${e}" --include "*.${E_UPPER}")
done

# Path substrings whose folders hold DICOM/medical-imaging exports (hundreds of
# raw slice images that aren't documents). These are EXCLUDED even if their
# extension is allowed — the real report PDFs sit alongside and are still kept.
# Loaded from options.exclude_path_patterns in the config; falls back to this
# default. Matching is case-insensitive on the object key.
DEFAULT_EXCLUDE_PATTERNS="ihe_pdi dicom osirix weasis webexport expimages viewer-windows viewer-macosx .app dicomdir"
if [ -f "${CONFIG_FILE}" ]; then
  CFG_EXCLUDES="$(python3 - "${CONFIG_FILE}" <<'PY'
import json, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    pats = (cfg.get("options", {}) or {}).get("exclude_path_patterns") or []
    pats = [str(p).strip() for p in pats if str(p).strip()]
    print("\n".join(pats))
except Exception:
    print("")
PY
)"
else
  CFG_EXCLUDES=""
fi
# Use config patterns if present, else the default. (newline-separated to allow
# patterns containing spaces.)
if [ -n "${CFG_EXCLUDES}" ]; then
  EXCLUDE_PATTERNS_RAW="${CFG_EXCLUDES}"
else
  EXCLUDE_PATTERNS_RAW="$(printf '%s\n' ${DEFAULT_EXCLUDE_PATTERNS})"
fi

# Build case-insensitive --exclude flags. For each pattern we add both a
# lowercase and an uppercase variant plus a *pattern* glob so it matches the
# substring anywhere in the path. Applied AFTER the includes so path exclusions
# win over type includes.
PATH_EXCLUDES=()
while IFS= read -r pat; do
  [ -n "${pat}" ] || continue
  PAT_L="$(printf '%s' "$pat" | tr '[:upper:]' '[:lower:]')"
  PAT_U="$(printf '%s' "$pat" | tr '[:lower:]' '[:upper:]')"
  PATH_EXCLUDES+=(--exclude "*${pat}*" --exclude "*${PAT_L}*" --exclude "*${PAT_U}*")
done <<EOF
${EXCLUDE_PATTERNS_RAW}
EOF
[ ${#PATH_EXCLUDES[@]} -gt 0 ] && echo "==> Excluding medical-imaging paths matching: $(printf '%s ' ${EXCLUDE_PATTERNS_RAW})"

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
  # Filter order matters (last match wins): exclude all, re-include doc types,
  # then re-exclude medical-imaging paths so slice dumps are dropped even though
  # their extension is allowed.
  SYNC_OUT="$(aws s3 sync "${ROOT}" "${DEST}" \
    --delete \
    --exclude "*" \
    "${INCLUDES[@]}" \
    ${PATH_EXCLUDES[@]+"${PATH_EXCLUDES[@]}"} \
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
