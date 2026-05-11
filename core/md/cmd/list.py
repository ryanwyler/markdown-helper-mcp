"""markdown_list: index of open + touched files, OR full status of one.

Without --filename: index of every workspace AND touched-only entry
visible from cwd. Touched entries (read via source:'disk' but never
opened for edit) appear with workspaceState='touched' and have no
stateCounts.

With --filename: detailed status of that one file (sections + state
counts + save staleness + running dispatches).

Optional --filter-states a,b,c filters at the file level: only files
with at least one section in any of the listed states pass through.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

from . import _common as c
from ..dispatch import reconcile_all_dispatched
from ..sections import fetch_all_sections_in_order
from ..storage import (
    file_db_path,
    file_workspace_exists,
    find_helper_root,
    get_schema,
    list_touched_workspaces,
    list_workspaces,
    meta_get,
    open_db,
    read_touched_entry,
    resolve_filename,
)
from ..util import now


def register_parser(sub) -> None:
    p = sub.add_parser("list",
                       help="index of all open + touched files, or full status of one")
    p.add_argument("--filename",
                   help="if set, return detailed status of just this file")
    p.add_argument("--filter-states",
                   help="comma-separated state names to filter by (file-level)")
    p.add_argument("--limit", type=int)
    c.add_pretty(p)
    p.set_defaults(func=cmd_list)


def cmd_list(args: argparse.Namespace) -> int:
    root = find_helper_root()

    if args.filename:
        try:
            resolved = resolve_filename(args.filename)
        except ValueError as e:
            c.emit_error({"error": str(e)})
            return 2
        canonical = resolved.canonical
        workspace_key = resolved.workspace_key
        if not file_workspace_exists(root, workspace_key):
            # Maybe it's a touched-only entry?
            touched = read_touched_entry(root, workspace_key)
            if touched is not None:
                entry = {
                    "filename": canonical,
                    "workspaceState": "touched",
                    "lastReadAt": touched.get("lastReadAt"),
                    "sha": touched.get("sha"),
                    "sizeBytes": touched.get("sizeBytes"),
                    "nextSteps": [
                        f"Open for editing: markdown_open {{ filename: {canonical!r} }}.",
                    ],
                }
                if resolved.is_foreign:
                    entry["readOnly"] = True
                c.emit(entry, args.pretty)
                return 0
            c.emit_error({
                "error": f"file {canonical!r} is not open and has not been read",
                "hint": "Use markdown_open or markdown_read source:'disk'.",
            })
            return 1
        entry = _build_file_summary(
            root, canonical, workspace_key,
            foreign=resolved.is_foreign, detailed=True,
        )
        if entry is None:
            c.emit_error({"error": f"could not read workspace for {canonical!r}"})
            return 1
        c.emit(entry, args.pretty)
        return 0

    # No filename: index across all workspaces + touched entries.
    files: list[dict[str, Any]] = []
    for canonical, workspace_key in list_workspaces(root):
        foreign = canonical.startswith("/")
        entry = _build_file_summary(
            root, canonical, workspace_key,
            foreign=foreign, detailed=False,
        )
        if entry is not None:
            files.append(entry)

    touched_entries: list[dict[str, Any]] = []
    for canonical, workspace_key in list_touched_workspaces(root):
        foreign = canonical.startswith("/")
        touched = read_touched_entry(root, workspace_key)
        if touched is None:
            continue
        entry = {
            "filename": canonical,
            "workspaceState": "touched",
            "lastReadAt": touched.get("lastReadAt"),
            "sha": touched.get("sha"),
            "sizeBytes": touched.get("sizeBytes"),
        }
        if foreign:
            entry["readOnly"] = True
        touched_entries.append(entry)

    if args.filter_states:
        wanted = {s.strip() for s in args.filter_states.split(",") if s.strip()}
        files = [
            f for f in files
            if any(f.get("stateCounts", {}).get(s, 0) > 0 for s in wanted)
        ]
        # touched entries don't have state counts; exclude under filter.
        touched_entries = []

    files.sort(key=lambda f: f.get("createdAt") or 0, reverse=True)
    touched_entries.sort(
        key=lambda f: f.get("lastReadAt") or 0, reverse=True,
    )
    if args.limit:
        files = files[: args.limit]
        touched_entries = touched_entries[: max(0, args.limit - len(files))]

    response = {
        "files": files,
        "touched": touched_entries,
    }
    c.emit(response, args.pretty)
    return 0


def _build_file_summary(
    root: Path, canonical: str, workspace_key: str, *,
    foreign: bool, detailed: bool,
) -> dict[str, Any] | None:
    db_path = file_db_path(root, workspace_key)
    if not db_path.exists():
        return None
    try:
        with open_db(db_path) as conn:
            created_at = meta_get(conn, "createdAt")
            schema = get_schema(conn)
            state_rows = conn.execute(
                "SELECT state, COUNT(*) FROM sections GROUP BY state"
            ).fetchall()
            counts = {r[0]: r[1] for r in state_rows}
            section_count = sum(counts.values())
            staleness = c.save_staleness(conn)

            entry: dict[str, Any] = {
                "filename": canonical,
                "workspaceState": "open",
                "schema": list(schema.names()),
                "sectionCount": section_count,
                "stateCounts": dict(sorted(counts.items())),
                "createdAt": int(created_at) if created_at and created_at.isdigit() else None,
                "needsSave": staleness["needsSave"],
                "sectionsChangedSinceSave": staleness["sectionsChangedSinceSave"],
                "lastSavedAt": staleness["lastSavedAt"],
            }
            if foreign:
                entry["readOnly"] = True
            next_steps: list[str] = []
            if not foreign:
                nudge = c.save_nudge_step(staleness, canonical)
                if nudge:
                    next_steps.append(nudge)

            if detailed:
                reconcile_all_dispatched(conn)
                sections = [
                    c.summarize_section(conn, s) for s in fetch_all_sections_in_order(conn)
                ]
                entry["sections"] = sections

                running_rows = conn.execute(
                    """SELECT a.job_id, a.section_id, a.session_id, a.pid, a.started_at
                       FROM assistants a
                       WHERE a.state IN ('queued', 'running')
                       ORDER BY a.started_at ASC"""
                ).fetchall()
                running: list[dict[str, Any]] = []
                from ..ids import compute_section_number
                for r in running_rows:
                    addr = compute_section_number(conn, r["section_id"])
                    running.append({
                        "jobId": r["job_id"],
                        "sectionNumber": addr.display,
                        "sessionId": r["session_id"],
                        "pid": r["pid"],
                        "startedAt": r["started_at"],
                        "ageSec": now() - r["started_at"],
                    })
                if running:
                    entry["runningDispatches"] = running
                    plural = "es" if len(running) != 1 else ""
                    next_steps.append(
                        f"{len(running)} dispatch{plural} running. "
                        f"Re-call markdown_list {{ filename: {canonical!r} }} to refresh."
                    )

            if next_steps:
                entry["nextSteps"] = next_steps
            return entry
    except sqlite3.Error:
        return None
