#!/usr/bin/env python3
"""Generate Bedrock Knowledge Base metadata sidecar files from the folder tree.

Documents are organised by topic and owner, e.g.
    <root>/<topic>/<owner>/[subfolders...]/file.ext
or
    <root>/<owner>/<topic>/[subfolders...]/file.ext

The folder ORDER can vary, so this script AUTO-DETECTS which segment is the
owner by matching it against a configurable list of known owners (loaded from
familia.config.json). For every document it writes a sibling

    <file.ext>.metadata.json

with filterable metadata that Bedrock indexes alongside the vectors:

    {
      "metadataAttributes": {
        "topic":       "...",
        "owner":       "...",             # normalised alias (e.g. "alba")
        "owner_name":  "...",             # display name from config, if known
        "subpath":     "...",             # remaining folders, slash-joined
        "doc_type":    "pdf",
        "file_name":   "file.ext",
        "source_path": "topic/owner/.../file.ext"
      }
    }

PRIVACY: the mapping of folder aliases to real names lives ONLY in
familia.config.json on your data machine (gitignored). This script and the repo
contain no personal data.

Owner detection is ROOT-INDEPENDENT: the script scans the whole path for the
first folder matching a known owner alias, then takes the topic from the folder
next to it. So the same document classifies identically whether you sync from
".../<topic>" or ".../<topic>/<owner>". Wrapper folders (e.g. the S3 sync prefix
"documents") are skipped when choosing the topic — see options.ignore_folders.

Config (familia.config.json, next to this script or via --config):
    {
      "owners": { "<folder-alias>": "<display name>", ... },
      "options": {
        "layout": "auto" | "topic_owner" | "owner_topic",
        "default_owner": "shared",
        "default_topic": "general",
        "ignore_folders": ["documents", "docs", "documentos"]
      }
    }

Usage:
    python3 generate_metadata.py <root> [<root> ...] [--config PATH]
                                 [--dry-run] [--prune]

    Accepts one or more local document roots (e.g. your "salud" and
    "Documentación" folders), processing each independently.

Idempotent. Re-running rewrites sidecars to match the CURRENT tree, so if you
reorganise folders the metadata updates on the next run. --prune removes
orphan sidecars whose document no longer exists.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata

SKIP_SUFFIXES = (".metadata.json",)
SKIP_NAMES = {".DS_Store", "Thumbs.db"}
MAX_VALUE_LEN = 512

DEFAULT_OPTIONS = {
    "layout": "auto",
    "default_owner": "shared",
    "default_topic": "general",
    # Wrapper/prefix folders that are NOT real topics (e.g. the S3 sync prefix).
    # When choosing the topic next to the owner, these are skipped.
    "ignore_folders": ["documents", "docs", "documentos"],
}


def _norm(value: str) -> str:
    """Lowercase + strip accents, for robust folder-name matching."""
    nfkd = unicodedata.normalize("NFKD", value)
    without_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return without_accents.strip().lower()


def _clean(value: str) -> str:
    return value.strip()[:MAX_VALUE_LEN]


def load_config(explicit_path: str | None, script_dir: str) -> dict:
    path = explicit_path or os.path.join(script_dir, "familia.config.json")
    if not os.path.isfile(path):
        print(
            f"Config not found: {path}\n"
            f"Copy familia.config.example.json to familia.config.json and fill "
            f"in your owner folder names.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)

    owners_raw = cfg.get("owners", {})
    # Map normalised alias -> display name.
    owners = {_norm(alias): name for alias, name in owners_raw.items()}
    options = {**DEFAULT_OPTIONS, **cfg.get("options", {})}
    if options["layout"] not in ("auto", "topic_owner", "owner_topic"):
        print(f"Invalid options.layout: {options['layout']}", file=sys.stderr)
        raise SystemExit(2)
    return {"owners": owners, "options": options}


def classify(folders: list[str], owners: dict, options: dict) -> tuple[str, str, str]:
    """Return (topic, owner_alias, subpath) from the folder segments.

    Auto-detection: whichever of the first two segments matches a known owner
    is the owner; the other is the topic. Falls back to the configured layout
    or defaults when neither matches.
    """
    layout = options["layout"]
    default_owner = options["default_owner"]
    default_topic = options["default_topic"]

    norm_folders = [_norm(f) for f in folders]

    def is_owner(n: str) -> bool:
        return n in owners

    # ---- Locate the owner ANYWHERE in the path -------------------------------
    # This makes classification independent of the local sync root: whether you
    # pass ".../<topic>" or ".../<topic>/<owner>" as the root, the owner is
    # found the same way. The first path segment that matches a known owner
    # alias (from familia.config.json) wins.
    owner_idx = next((i for i, n in enumerate(norm_folders) if is_owner(n)), None)

    ignore = {_norm(f) for f in options.get("ignore_folders", [])}

    def usable(seg: str) -> bool:
        return bool(seg) and _norm(seg) not in ignore

    if owner_idx is not None:
        owner = norm_folders[owner_idx]
        # Topic = the folder next to the owner, skipping wrapper folders like the
        # sync prefix. Respect layout; in "auto", prefer the folder BEFORE the
        # owner (topic/owner), else the folder AFTER (owner/topic).
        before = folders[owner_idx - 1] if owner_idx >= 1 else ""
        after = folders[owner_idx + 1] if owner_idx + 1 < len(folders) else ""
        before_ok = usable(before)
        after_ok = usable(after)

        if layout == "owner_topic":
            topic = after if after_ok else (before if before_ok else default_topic)
            used_after = after_ok
        elif layout == "topic_owner":
            topic = before if before_ok else (after if after_ok else default_topic)
            used_after = (not before_ok) and after_ok
        else:  # auto: topic/owner is the common case, so prefer 'before'
            topic = before if before_ok else (after if after_ok else default_topic)
            used_after = (not before_ok) and after_ok

        # subpath = everything after the owner, minus the folder used as topic.
        tail = folders[owner_idx + 2:] if used_after else folders[owner_idx + 1:]
        subpath = "/".join(tail)
    else:
        # No known owner in the path: fall back to positional heuristics on the
        # first two folders, honouring the configured layout.
        seg0 = folders[0] if len(folders) >= 1 else ""
        seg1 = folders[1] if len(folders) >= 2 else ""
        if layout == "owner_topic":
            owner = _norm(seg0) or default_owner
            topic = seg1 or default_topic
            subpath = "/".join(folders[2:])
        else:  # topic_owner or auto
            topic = seg0 or default_topic
            owner = _norm(seg1) or default_owner
            subpath = "/".join(folders[2:])

    owner_alias = _norm(owner) if owner else default_owner
    topic = topic or default_topic
    return _clean(topic), _clean(owner_alias), _clean(subpath)


def is_document(name: str) -> bool:
    if name in SKIP_NAMES or name.startswith("."):
        return False
    return not name.endswith(SKIP_SUFFIXES)


def build_payload(root: str, file_path: str, cfg: dict) -> dict:
    rel = os.path.relpath(file_path, root)
    parts = rel.split(os.sep)
    file_name = parts[-1]
    folders = parts[:-1]

    topic, owner_alias, subpath = classify(folders, cfg["owners"], cfg["options"])
    ext = os.path.splitext(file_name)[1].lstrip(".").lower() or "unknown"

    attrs = {
        "topic": topic,
        "owner": owner_alias,
        "doc_type": ext,
        "file_name": _clean(file_name),
        "source_path": _clean(rel.replace(os.sep, "/")),
    }
    display = cfg["owners"].get(owner_alias)
    if display:
        attrs["owner_name"] = _clean(display)
    if subpath:
        attrs["subpath"] = subpath
    return {"metadataAttributes": attrs}


def process_root(root: str, cfg: dict, dry_run: bool, prune: bool) -> tuple[int, int, int]:
    """Generate/update sidecars for one root. Returns (written, changed, pruned)."""
    written = changed = pruned = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        docs = {f for f in filenames if is_document(f)}

        for name in sorted(docs):
            file_path = os.path.join(dirpath, name)
            payload = build_payload(root, file_path, cfg)
            new_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            sidecar = file_path + ".metadata.json"

            # Detect whether the metadata changed (e.g. folder structure moved).
            old_text = None
            if os.path.isfile(sidecar):
                with open(sidecar, encoding="utf-8") as fh:
                    old_text = fh.read()
            is_change = old_text != new_text

            if dry_run:
                if is_change:
                    print(f"[dry-run] {'update' if old_text else 'create'}: {sidecar}")
            elif is_change:
                with open(sidecar, "w", encoding="utf-8") as fh:
                    fh.write(new_text)
            written += 1
            if is_change:
                changed += 1

        if prune:
            for name in filenames:
                if name.endswith(".metadata.json"):
                    doc = name[: -len(".metadata.json")]
                    if doc not in docs:
                        orphan = os.path.join(dirpath, name)
                        if dry_run:
                            print(f"[dry-run] prune orphan: {orphan}")
                        else:
                            os.remove(orphan)
                        pruned += 1
    return written, changed, pruned


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="+", help="One or more local document roots to process")
    ap.add_argument("--config", help="Path to familia.config.json (default: next to this script)")
    ap.add_argument("--dry-run", action="store_true", help="Print what would change, write nothing")
    ap.add_argument("--prune", action="store_true", help="Delete orphan .metadata.json files")
    args = ap.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    cfg = load_config(args.config, script_dir)

    total_written = total_changed = total_pruned = 0
    for raw in args.roots:
        root = os.path.abspath(raw)
        if not os.path.isdir(root):
            print(f"!! Skipping '{raw}': not a directory", file=sys.stderr)
            continue
        print(f"== Root: {root}")
        w, c, p = process_root(root, cfg, args.dry_run, args.prune)
        total_written += w
        total_changed += c
        total_pruned += p

    verb = "Would process" if args.dry_run else "Processed"
    print(f"{verb} {total_written} document(s); {total_changed} metadata change(s).", end="")
    if args.prune:
        print(f" {total_pruned} orphan(s) pruned.", end="")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
