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
        "ignore_folders": ["documents", "docs", "documentos"],
        "allowed_extensions": ["pdf", "docx", "jpg", ...]
      }
    }

Sidecars are only generated for files whose extension is in allowed_extensions
(kept in sync with sync_docs.sh so metadata is not created for files that won't
be uploaded). An empty/absent list means all files are processed. With --prune,
sidecars for now-disallowed files are removed.

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
import datetime
import json
import os
import struct
import sys
import unicodedata

SKIP_SUFFIXES = (".metadata.json",)
SKIP_NAMES = {".DS_Store", "Thumbs.db"}
MAX_VALUE_LEN = 512

_JPEG_EXTS = {"jpg", "jpeg"}


def _exif_datetime_original(path: str) -> str:
    """Extract EXIF DateTimeOriginal from a JPEG as an ISO date (YYYY-MM-DD).

    Pure-Python, no dependencies: scans APP1/Exif, reads the TIFF IFD0 + Exif
    sub-IFD for tag 0x9003 (DateTimeOriginal) or 0x0132 (DateTime). Returns ""
    on any parsing issue — enrichment is best-effort and never fatal.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(131072)  # EXIF lives near the top; 128KB is ample
        if data[0:2] != b"\xff\xd8":  # not a JPEG
            return ""
        # Find APP1 (Exif) segment.
        i = 2
        exif = None
        while i + 4 < len(data):
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            seg = data[i + 4:i + 2 + seg_len]
            if marker == 0xE1 and seg[:6] == b"Exif\x00\x00":
                exif = seg[6:]
                break
            if marker == 0xDA:  # start of scan — no more headers
                break
            i += 2 + seg_len
        if not exif:
            return ""

        # TIFF header: byte order + IFD0 offset.
        bo = "<" if exif[:2] == b"II" else ">"
        ifd0 = struct.unpack(bo + "I", exif[4:8])[0]

        def read_ifd(offset):
            entries = {}
            if offset + 2 > len(exif):
                return entries, 0
            count = struct.unpack(bo + "H", exif[offset:offset + 2])[0]
            p = offset + 2
            for _ in range(count):
                if p + 12 > len(exif):
                    break
                tag, typ, cnt = struct.unpack(bo + "HHI", exif[p:p + 8])
                val = exif[p + 8:p + 12]
                entries[tag] = (typ, cnt, val)
                p += 12
            next_ifd = struct.unpack(bo + "I", exif[p:p + 4])[0] if p + 4 <= len(exif) else 0
            return entries, next_ifd

        def ascii_at(entry):
            typ, cnt, val = entry
            if typ != 2:  # ASCII
                return ""
            if cnt <= 4:
                raw = val[:cnt]
            else:
                off = struct.unpack(bo + "I", val)[0]
                raw = exif[off:off + cnt]
            return raw.split(b"\x00", 1)[0].decode("ascii", "ignore")

        e0, _ = read_ifd(ifd0)
        # Exif sub-IFD pointer (tag 0x8769) → where DateTimeOriginal lives.
        dt = ""
        if 0x8769 in e0:
            sub_off = struct.unpack(bo + "I", e0[0x8769][2])[0]
            esub, _ = read_ifd(sub_off)
            if 0x9003 in esub:
                dt = ascii_at(esub[0x9003])
        if not dt and 0x0132 in e0:  # fall back to file DateTime
            dt = ascii_at(e0[0x0132])
        if not dt:
            return ""
        # EXIF format: "YYYY:MM:DD HH:MM:SS" → ISO date.
        date_part = dt.strip().split(" ")[0].replace(":", "-")
        # sanity check
        datetime.date.fromisoformat(date_part)
        return date_part
    except Exception:
        return ""


# Default extension -> content_type mapping. Documents are the common case;
# media types are here so mixed content (photos/films/music) auto-classifies
# once you start importing PhotoPrism/Plex records. Override/extend via
# options.content_type_map in familia.config.json.
DEFAULT_CONTENT_TYPES = {
    "photo": ["jpg", "jpeg", "png", "gif", "bmp", "tiff", "webp", "heic"],
    "film": ["mp4", "mkv", "avi", "mov", "wmv", "m4v"],
    "music": ["mp3", "flac", "wav", "m4a", "aac", "ogg"],
    # everything else falls back to "document"
}


def content_type_for(ext: str, options: dict) -> str:
    """Map a file extension to a content_type. Config's content_type_map (if
    present) is merged over the default mapping."""
    mapping = dict(DEFAULT_CONTENT_TYPES)
    for ctype, exts in (options.get("content_type_map") or {}).items():
        mapping[str(ctype)] = [str(e).lstrip(".").strip().lower() for e in exts]
    for ctype, exts in mapping.items():
        if ext in exts:
            return ctype
    return options.get("default_content_type", "document")


def file_enrichment(path: str, ext: str) -> dict:
    """Best-effort file properties for the metadata: modified date, size, and
    (for JPEGs) the EXIF capture date. Any failure is silently skipped."""
    out = {}
    try:
        st = os.stat(path)
        out["modified_date"] = datetime.date.fromtimestamp(st.st_mtime).isoformat()
        out["file_size_kb"] = int(round(st.st_size / 1024))
    except Exception:
        pass
    if ext in _JPEG_EXTS:
        captured = _exif_datetime_original(path)
        if captured:
            out["captured_date"] = captured
    return out

DEFAULT_OPTIONS = {
    "layout": "auto",
    "default_owner": "shared",
    "default_topic": "general",
    # Wrapper/prefix folders that are NOT real topics (e.g. the S3 sync prefix).
    # When choosing the topic next to the owner, these are skipped.
    "ignore_folders": ["documents", "docs", "documentos"],
    # File extensions to generate sidecars for. Kept in sync with the upload
    # allowlist so we don't create metadata for files that won't be uploaded.
    # Empty list => generate for ALL files (no extension filtering).
    "allowed_extensions": [],
    # Path substrings (case-insensitive) whose files are skipped even if the
    # extension is allowed — e.g. DICOM/medical-imaging slice-export folders.
    # Kept in sync with sync_docs.sh's exclude_path_patterns.
    "exclude_path_patterns": [],
    # Add file-property metadata (modified_date, file_size_kb, and EXIF
    # captured_date for JPEGs) to each sidecar. Set false to disable.
    "enrich_file_metadata": True,
    # Discriminator fields (small, filterable, future-proof for mixed media):
    #   media_source  — provenance tag: familia | photoprism | plex | ...
    #   default_content_type — used when the extension matches no media type
    #   content_type_map — override/extend the extension->content_type mapping
    "media_source": "familia",
    "default_content_type": "document",
    "content_type_map": {},
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
    # Normalise allowed_extensions: lowercase, strip leading dot, drop blanks.
    options["allowed_extensions"] = [
        str(e).lstrip(".").strip().lower()
        for e in (options.get("allowed_extensions") or [])
        if str(e).strip()
    ]
    # Normalise exclude_path_patterns: lowercase, drop blanks (matched against
    # the lowercased relative path).
    options["exclude_path_patterns"] = [
        str(p).strip().lower()
        for p in (options.get("exclude_path_patterns") or [])
        if str(p).strip()
    ]
    return {"owners": owners, "options": options}


def classify(folders: list[str], owners: dict, options: dict,
             topic_hint: str = "") -> tuple[str, str, str]:
    """Return (topic, owner_alias, subpath) from the folder segments.

    Auto-detection: whichever segment matches a known owner is the owner; the
    adjacent folder is the topic. When the owner is the first folder inside the
    root (so there is no topic folder in the relative path), `topic_hint` — the
    root's own folder name — is used as the topic. Falls back to configured
    layout / defaults otherwise.
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

    # The root's own folder name is a topic candidate (used when the owner is
    # the first folder inside the root, so no topic folder appears in the path).
    hint = topic_hint if usable(topic_hint) else ""

    if owner_idx is not None:
        owner = norm_folders[owner_idx]
        # Topic = the folder next to the owner, skipping wrapper folders like the
        # sync prefix. Respect layout; in "auto", prefer the folder BEFORE the
        # owner (topic/owner), else the folder AFTER (owner/topic), else the
        # root basename hint, else the default.
        before = folders[owner_idx - 1] if owner_idx >= 1 else ""
        after = folders[owner_idx + 1] if owner_idx + 1 < len(folders) else ""
        before_ok = usable(before)
        after_ok = usable(after)

        if layout == "owner_topic":
            # owner/topic: the folder AFTER the owner is the topic; anything
            # beyond that is subpath.
            topic = after if after_ok else (before if before_ok else (hint or default_topic))
            used_after = after_ok
        else:
            # topic_owner / auto: the topic is the folder BEFORE the owner, or
            # the root-name hint when the owner is the first folder. Everything
            # AFTER the owner is subpath (never the topic).
            topic = before if before_ok else (hint or default_topic)
            used_after = False

        # subpath = everything after the owner, minus the folder used as topic
        # (only in owner/topic layout, where 'after' was consumed as the topic).
        tail = folders[owner_idx + 2:] if used_after else folders[owner_idx + 1:]
        subpath = "/".join(tail)
    else:
        # No known owner anywhere in the path -> shared document. The topic is
        # the root-name hint (the folder each root maps to, e.g. "salud") when
        # available, else the first usable folder; everything else is subpath.
        owner = default_owner
        if hint:
            topic = hint
            subpath = "/".join(folders)
        else:
            seg0 = folders[0] if len(folders) >= 1 else ""
            topic = seg0 if usable(seg0) else default_topic
            subpath = "/".join(folders[1:]) if usable(seg0) else "/".join(folders)

    owner_alias = _norm(owner) if owner else default_owner
    topic = topic or default_topic
    return _clean(topic), _clean(owner_alias), _clean(subpath)


def is_document(name: str, allowed_exts: set = None) -> bool:
    if name in SKIP_NAMES or name.startswith("."):
        return False
    if name.endswith(SKIP_SUFFIXES):
        return False
    # If an allowlist is configured, only treat matching extensions as
    # documents (so we don't generate sidecars for files that won't be
    # uploaded). Empty/None allowlist => accept everything.
    if allowed_exts:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        return ext in allowed_exts
    return True


def build_payload(root: str, file_path: str, cfg: dict) -> dict:
    rel = os.path.relpath(file_path, root)
    parts = rel.split(os.sep)
    file_name = parts[-1]
    folders = parts[:-1]

    topic, owner_alias, subpath = classify(
        folders, cfg["owners"], cfg["options"], topic_hint=os.path.basename(root)
    )
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

    # File-property enrichment (dates/size) unless disabled in config. These
    # give the model temporal context (e.g. to prefer the most recent DNI) and
    # enable date-range filtering. Kept filterable (small values).
    if cfg["options"].get("enrich_file_metadata", True):
        attrs.update(file_enrichment(file_path, ext))

    # ---- Discriminator fields (small, filterable, future-proof) -------------
    # content_type: master discriminator once media is mixed in. Auto-derived
    # from the extension, over/extended via options.content_type_map.
    attrs["content_type"] = content_type_for(ext, cfg["options"])
    # media_source: where this record came from (familia | photoprism | plex).
    attrs["media_source"] = cfg["options"].get("media_source", "familia")
    # year: derived from the best available date (captured > modified). Cheap,
    # high-value filter ("cosas de 2019"). Only set when a date is known.
    date_for_year = attrs.get("captured_date") or attrs.get("modified_date")
    if date_for_year and len(date_for_year) >= 4 and date_for_year[:4].isdigit():
        attrs["year"] = date_for_year[:4]

    # NOTE: `place` (city/country) is part of the canonical schema but is NOT
    # set by this pipeline — it has no reliable source for it (we never
    # reverse-geocode GPS). The PhotoPrism importer will populate `place` per
    # photo from PhotoPrism's already-geocoded place names. S3 Vectors needs no
    # pre-declaration, so the field simply appears (filterable) once set there.

    return {"metadataAttributes": attrs}


def process_root(root: str, cfg: dict, dry_run: bool, prune: bool,
                 output_dir: str = "") -> tuple[int, int, int]:
    """Generate/update sidecars for one root. Returns (written, changed, pruned).

    If output_dir is set, sidecars are written there (mirroring each file's path
    relative to root) instead of next to the source documents, keeping the
    source folders clean. Otherwise they are written next to each document.
    """
    allowed_exts = set(cfg["options"].get("allowed_extensions") or [])
    exclude_patterns = cfg["options"].get("exclude_path_patterns") or []
    written = changed = pruned = 0

    def sidecar_path(file_path):
        # Where the sidecar for this document goes.
        if output_dir:
            rel = os.path.relpath(file_path, root)
            return os.path.join(output_dir, rel + ".metadata.json")
        return file_path + ".metadata.json"

    for dirpath, _dirnames, filenames in os.walk(root):
        # Skip files under excluded paths (DICOM/medical-imaging exports) even
        # if their extension is allowed. Matched on the lowercased rel path.
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/").lower()

        def _excluded(name):
            hay = (rel_dir + "/" + name.lower())
            return any(p in hay for p in exclude_patterns)

        docs = {
            f for f in filenames
            if is_document(f, allowed_exts) and not _excluded(f)
        }

        for name in sorted(docs):
            file_path = os.path.join(dirpath, name)
            payload = build_payload(root, file_path, cfg)
            new_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            sidecar = sidecar_path(file_path)

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
                os.makedirs(os.path.dirname(sidecar), exist_ok=True)
                with open(sidecar, "w", encoding="utf-8") as fh:
                    fh.write(new_text)
            written += 1
            if is_change:
                changed += 1

        # In-place mode: prune orphan sidecars sitting next to documents.
        if prune and not output_dir:
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

    # Staging-dir mode: prune sidecars in the output dir whose source document
    # no longer exists (removed or now filtered out). We recompute which source
    # docs are eligible and drop any staged sidecar without a match.
    if prune and output_dir and os.path.isdir(output_dir):
        eligible = set()
        for dp, _dn, fns in os.walk(root):
            rd = os.path.relpath(dp, root).replace(os.sep, "/").lower()
            for f in fns:
                hay = rd + "/" + f.lower()
                if is_document(f, allowed_exts) and not any(p in hay for p in exclude_patterns):
                    eligible.add(os.path.relpath(os.path.join(dp, f), root))
        for dp, _dn, fns in os.walk(output_dir):
            for f in fns:
                if not f.endswith(".metadata.json"):
                    continue
                staged = os.path.join(dp, f)
                rel_doc = os.path.relpath(staged, output_dir)[: -len(".metadata.json")]
                if rel_doc not in eligible:
                    if dry_run:
                        print(f"[dry-run] prune orphan: {staged}")
                    else:
                        os.remove(staged)
                    pruned += 1

    return written, changed, pruned


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="+", help="One or more local document roots to process")
    ap.add_argument("--config", help="Path to familia.config.json (default: next to this script)")
    ap.add_argument("--dry-run", action="store_true", help="Print what would change, write nothing")
    ap.add_argument("--prune", action="store_true", help="Delete orphan .metadata.json files")
    ap.add_argument("--output-dir", help="Write sidecars into this directory (mirroring paths relative to the root) instead of next to the documents. Keeps source folders clean.")
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
        out = os.path.abspath(args.output_dir) if args.output_dir else ""
        w, c, p = process_root(root, cfg, args.dry_run, args.prune, output_dir=out)
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
