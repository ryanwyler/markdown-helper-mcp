"""markdown_delete: remove a section (and optionally its descendants).

Heals the chain so the section's successor points at its predecessor.
By default deletes descendants too (recursive=true).

Refuses if the section is in any working state and a sub-agent is
running (any assistants row in queued/running) unless --force.

Returns the standard changeReport + userSummary.
"""

from __future__ import annotations

import argparse

from . import _common as c
from ..sections import delete_section, fetch_children, fetch_section
from ..states import STATE_READONLY
from ..storage import open_db


def register_parser(sub) -> None:
    p = sub.add_parser("delete",
                       help="remove a section (heals the chain; deletes descendants by default)")
    p.add_argument("--filename", required=True)
    p.add_argument("--section-number", required=True,
                   help="sectionNumber OR title.")
    p.add_argument("--parent-section",
                   help="scope title-resolution to the named section's "
                        "subtree (use when --section-number is a title "
                        "that matches multiple sections).")
    p.add_argument("--no-recursive", dest="recursive", action="store_false",
                   default=True,
                   help="refuse if the section has children")
    p.add_argument("--force", action="store_true",
                   help="delete even if a sub-agent is currently running on it")
    c.add_pretty(p)
    p.set_defaults(func=cmd_delete)


def cmd_delete(args: argparse.Namespace) -> int:
    resolved = c.resolve_workspace_or_die(args, require_writable=True)
    if resolved is None:
        return 1
    _, ws_dir, filename = resolved

    with open_db(ws_dir / "db.sqlite3") as conn:
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

        if sec.state == STATE_READONLY:
            c.emit_error({
                "error": f"section {args.section_number!r} is read-only",
                "readOnly": True,
            })
            return 1

        # Active dispatch check (any running assistants row on this section).
        running = conn.execute(
            "SELECT COUNT(*) AS c FROM assistants "
            "WHERE section_id=? AND state IN ('queued', 'running')",
            (uuid,),
        ).fetchone()
        if running and running["c"] > 0 and not args.force:
            c.emit_error({
                "error": f"section {args.section_number!r} has a running sub-agent",
                "hint": "Use markdown_dispatch { kill: true } first, "
                        "or pass force=true to delete anyway.",
            })
            return 1

        if not args.recursive:
            children = fetch_children(conn, uuid)
            if children:
                c.emit_error({
                    "error": f"section {args.section_number!r} has {len(children)} children",
                    "hint": "Pass recursive=true (default) to delete them too, "
                            "or move/delete them first.",
                })
                return 1

        before_snapshot = c.take_outline_snapshot(conn)

        try:
            count = delete_section(conn, uuid, recursive=args.recursive)
        except ValueError as e:
            c.emit_error({"error": str(e)})
            return 1

        after_snapshot = c.take_outline_snapshot(conn)
        report = c.compute_change_report(
            before_snapshot, after_snapshot, filename,
            conn=conn, by="primary",
        )

    response = {
        "filename": filename,
        "deletedCount": count,
        "changeReport": report["changeReport"],
        "userSummary": report["userSummary"],
    }
    c.emit(response, args.pretty)
    return 0
