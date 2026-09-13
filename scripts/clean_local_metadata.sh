#!/usr/bin/env bash
# Removes stray *.metadata.json sidecar files from your LOCAL document folders.
#
# Older versions of sync_docs.sh wrote sidecars next to your documents. The
# current version stages them in a temp dir instead (see sync_docs.sh), so any
# .metadata.json still sitting in your local tree is leftover and safe to delete
# — they are always regenerated at sync time and only need to exist in S3.
#
# SAFE BY DEFAULT: this only ever targets files ending in ".metadata.json" and
# runs as a DRY RUN unless you pass --apply. It never touches your documents.
#
# Usage:
#   ./clean_local_metadata.sh <root> [<root> ...]            # dry run (lists only)
#   ./clean_local_metadata.sh --apply <root> [<root> ...]    # actually delete
#
# Examples:
#   ./clean_local_metadata.sh "$HOME/Docs/Family" "$HOME/Scans"
#   ./clean_local_metadata.sh --apply "$HOME/Docs/Family"
set -euo pipefail

APPLY=0
if [ "${1:-}" = "--apply" ]; then
  APPLY=1
  shift
fi
[ "$#" -ge 1 ] || { echo "Usage: clean_local_metadata.sh [--apply] <root> [<root> ...]" >&2; exit 1; }

if [ "${APPLY}" -eq 0 ]; then
  echo "*** DRY RUN — listing what WOULD be deleted. Re-run with --apply to delete. ***"
fi

total=0
for ROOT in "$@"; do
  if [ ! -d "${ROOT}" ]; then
    echo "!! Skipping '${ROOT}': not a directory" >&2
    continue
  fi
  echo "==================================================================="
  echo "Root: ${ROOT}"

  # Count first (portable find; -type f guards against odd names).
  count="$(find "${ROOT}" -type f -name '*.metadata.json' | wc -l | tr -d ' ')"
  echo "  Found ${count} .metadata.json file(s)."

  if [ "${count}" -gt 0 ]; then
    if [ "${APPLY}" -eq 1 ]; then
      # -print then -delete so you see what was removed.
      find "${ROOT}" -type f -name '*.metadata.json' -print -delete
    else
      # Show a sample so you can sanity-check without flooding the terminal.
      find "${ROOT}" -type f -name '*.metadata.json' | head -20
      [ "${count}" -gt 20 ] && echo "  ... (${count} total; showing first 20)"
    fi
  fi
  total=$((total + count))
done

echo "==================================================================="
if [ "${APPLY}" -eq 1 ]; then
  echo "==> Deleted ${total} .metadata.json file(s) from local folders."
else
  echo "==> DRY RUN: ${total} .metadata.json file(s) would be deleted. Re-run with --apply."
fi
