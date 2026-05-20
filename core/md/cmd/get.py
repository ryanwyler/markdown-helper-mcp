"""markdown_get: read one section's full content + history.

EXPLICIT SOURCE: --source workspace|disk.

  workspace : returns content + seed + revision history (last 10).
  disk      : returns content only (disk has no per-section metadata).
              Side-effect: writes touched.json marker.
"""

from __future__ import annotations

import argparse
import hashlib

from . import _common as c
from ..parse import flatten_outline, parse_disk_outline
from ..revisions import fetch_recent_revisions
from ..sections import fetch_section
from ..storage import (
    file_db_path,
    file_workspace_exists,
    find_helper_root,
    open_db,
    resolve_filename,
    write_touched_entry,
)
from ..util import count_words, now


def register_parser(sub) -> None:
    p = sub.add_parser("get",
                       help="read one section's content + seed + history")
    p.add_argument("--filename", required=True)
    p.add_argument("--section-number", required=True,
                   help="sectionNumber OR title. When passing a title that "
                        "is ambiguous (matches multiple sections), narrow "
                        "with --parent-section.")
    p.add_argument("--parent-section",
                   help="scope title-resolution to the named section's "
                        "subtree. Used when --section-number is a title "
                        "that matches multiple sections.")
    p.add_argument("--source", choices=["workspace", "disk"], required=True)
    c.add_pretty(p)
    p.set_defaults(func=cmd_get)


def cmd_get(args: argparse.Namespace) -> int:
    if not args.filename:
        c.emit_error({"error": "filename is required"})
        return 2
    try:
        resolved = resolve_filename(args.filename)
    except ValueError as e:
        c.emit_error({"error": str(e)})
        return 2
    filename = resolved.canonical
    helper_root = find_helper_root()

    if args.source == "workspace":
        if not file_workspace_exists(helper_root, resolved.workspace_key):
            c.emit_error({
                "error": f"file {filename!r} is not open in the workspace",
                "hint": f"Open with markdown_open or read source:'disk'.",
            })
            return 1

        with open_db(file_db_path(helper_root, resolved.workspace_key)) as conn:
            uuid = c.resolve_section_handle(
                conn, args.section_number,
                parent_scope=args.parent_section,
                arg_name="section-number",
            )
            if uuid is None:
                return 1
            sec = fetch_section(conn, uuid)
            if sec is None:
                c.emit_error({"error": f"section {args.section_number!r} not found"})
                return 1

            response = c.summarize_section(conn, sec, include_content=True)
            response["filename"] = filename
            response["source"] = "workspace"
            response["history"] = fetch_recent_revisions(conn, uuid, limit=10)
            if resolved.is_foreign:
                response["readOnly"] = True

        c.emit(response, args.pretty)
        return 0

    # source == "disk"
    if not resolved.disk_path.exists():
        c.emit_error({"error": f"not found: {filename!r}"})
        return 1

    roots = parse_disk_outline(resolved.disk_path)
    flat = flatten_outline(roots)
    target = next((s for s in flat if s.section_number == args.section_number), None)
    if target is None:
        c.emit_error({
            "error": f"sectionNumber {args.section_number!r} not found in disk parse",
            "hint": "Use markdown_outline source:'disk' to see current numbers.",
        })
        return 1

    # Touched.json side effect.
    try:
        data = resolved.disk_path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        write_touched_entry(
            helper_root, resolved.workspace_key,
            sha=sha, size_bytes=len(data),
            last_read_at=now(),
        )
    except OSError:
        pass

    response = {
        "filename": filename,
        "source": "disk",
        "sectionNumber": target.section_number,
        "depth": target.depth,
        "title": target.title,
        "wordCount": count_words(target.body),
        "content": target.body,
    }
    if resolved.is_foreign:
        response["readOnly"] = True
    c.emit(response, args.pretty)
    return 0
