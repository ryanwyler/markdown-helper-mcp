"""markdown_outline: scoreboard view of all sections in a file.

EXPLICIT SOURCE: --source workspace|disk (no auto). Mutations remain
workspace-only; reads are explicit about which view they want.

  --source workspace : pulls from the workspace cache, includes state
                       machine fields, save staleness, nextFocus.
  --source disk      : parses the file fresh; no state machine, no
                       save staleness. Side-effect: writes touched.json.

Optional --filter-states a,b,c : show only sections whose state is in
the list (workspace source only).
"""

from __future__ import annotations

import argparse
import hashlib

from . import _common as c
from ..dispatch import reconcile_all_dispatched
from ..parse import flatten_outline, parse_disk_outline
from ..sections import fetch_outline_tree
from ..states import PSEUDO_STATES, STATE_LOADED, STATE_READONLY
from ..storage import (
    file_db_path,
    file_workspace_exists,
    find_helper_root,
    get_schema,
    open_db,
    resolve_filename,
    write_touched_entry,
)
from ..util import count_words, now


def register_parser(sub) -> None:
    p = sub.add_parser("outline",
                       help="show all sections + state + save staleness")
    p.add_argument("--filename", required=True)
    p.add_argument("--source", choices=["workspace", "disk"], required=True,
                   help="explicit source. workspace requires file open. "
                        "disk parses fresh and writes a touched.json marker.")
    p.add_argument("--filter-states",
                   help="comma-separated list of state names to filter by "
                        "(workspace source only)")
    c.add_pretty(p)
    p.set_defaults(func=cmd_outline)


def cmd_outline(args: argparse.Namespace) -> int:
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
    workspace_key = resolved.workspace_key

    if args.source == "workspace":
        if not file_workspace_exists(helper_root, workspace_key):
            c.emit_error({
                "error": f"file {filename!r} is not open in the workspace",
                "hint": f"Open it with markdown_open {{ filename: {filename!r} }}, "
                        f"or read structure from disk with source: 'disk'.",
            })
            return 1
        return _outline_workspace(args, filename, workspace_key, helper_root,
                                  foreign=resolved.is_foreign)

    # source == "disk"
    return _outline_disk(args, filename, resolved.disk_path, helper_root,
                         workspace_key=workspace_key,
                         foreign=resolved.is_foreign)


# ---------------------------------------------------------------------------
# Workspace mode
# ---------------------------------------------------------------------------

def _outline_workspace(args, filename: str, workspace_key: str,
                       helper_root, *, foreign: bool) -> int:
    from ..storage import file_dir as _file_dir
    ws_dir = _file_dir(helper_root, workspace_key)
    filter_states: set[str] | None = None
    if args.filter_states:
        filter_states = {s.strip() for s in args.filter_states.split(",") if s.strip()}

    with open_db(ws_dir / "db.sqlite3") as conn:
        reconcile_all_dispatched(conn)
        roots = fetch_outline_tree(conn)
        staleness = c.save_staleness(conn)
        schema = get_schema(conn)
        running_dispatches = conn.execute(
            "SELECT COUNT(*) AS c FROM assistants "
            "WHERE state IN ('queued', 'running')"
        ).fetchone()["c"]

    flat = flatten_outline(roots)
    if filter_states is not None:
        flat = [s for s in flat if s.state in filter_states]

    section_dicts = [_serialize_section(s, include_state=True) for s in flat]

    counts: dict[str, int] = {}
    for s in flat:
        if s.state:
            counts[s.state] = counts.get(s.state, 0) + 1

    response: dict = {
        "filename": filename,
        "source": "workspace",
        "schema": list(schema.names()),
        "sectionCount": len(flat),
        "stateCounts": dict(sorted(counts.items())),
        "sections": section_dicts,
        "needsSave": staleness["needsSave"],
        "sectionsChangedSinceSave": staleness["sectionsChangedSinceSave"],
        "lastSavedAt": staleness["lastSavedAt"],
    }
    if foreign:
        response["readOnly"] = True

    # nextFocus: first section in render order that is in the schema's
    # initial state OR needsAttention state OR loaded pseudo-state.
    target_states = {STATE_LOADED}
    init = schema.initial_state()
    if init:
        target_states.add(init)
    needs = schema.needs_attention_state()
    if needs:
        target_states.add(needs)
    for s in flat:
        if s.state in target_states:
            response["nextFocus"] = {
                "sectionNumber": s.section_number,
                "title": s.title,
                "state": s.state,
                "seed": s.seed,
            }
            break

    next_steps: list[str] = []
    # Active dispatches: only nudge when there's a real `assistants`
    # row in queued/running. A section sitting in the working-state
    # without a live row was set there manually -- agent's call.
    if running_dispatches > 0:
        plural = "es" if running_dispatches != 1 else ""
        next_steps.append(
            f"{running_dispatches} dispatch{plural} running. "
            f"Re-call markdown_outline to see them flip when done, or "
            f"markdown_dispatch {{ kill: true }} to abort."
        )
    if not foreign:
        nudge = c.save_nudge_step(staleness, filename)
        if nudge:
            next_steps.append(nudge)
    if next_steps:
        response["nextSteps"] = next_steps

    c.emit(response, args.pretty)
    return 0


# ---------------------------------------------------------------------------
# Disk mode
# ---------------------------------------------------------------------------

def _outline_disk(args, filename: str, disk_path,
                  helper_root, *,
                  workspace_key: str, foreign: bool) -> int:
    if not disk_path.exists():
        c.emit_error({
            "error": f"not found: {filename!r} (looked at {disk_path}). "
                     f"Save to write to disk.",
        })
        return 1

    roots = parse_disk_outline(disk_path)
    flat = flatten_outline(roots)
    section_dicts = [_serialize_section(s, include_state=False) for s in flat]

    # Touched.json side effect.
    try:
        data = disk_path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        write_touched_entry(
            helper_root, workspace_key,
            sha=sha, size_bytes=len(data),
            last_read_at=now(),
        )
    except OSError:
        pass

    open_hint = (
        f"Open the file for read-only inspection: markdown_open "
        f"{{ filename: {filename!r} }}. To edit, save it locally: "
        f"markdown_save {{ filename: {filename!r}, asFilename: 'docs/<name>.md' }}."
        if foreign else
        f"Open the file for editing: markdown_open {{ filename: {filename!r} }}."
    )
    response: dict = {
        "filename": filename,
        "source": "disk",
        "sectionCount": len(flat),
        "sections": section_dicts,
        "nextSteps": [
            f"Read sections: markdown_read {{ filename: {filename!r}, "
            f"sections: 'A.1,A.2-A.4', source: 'disk' }}.",
            f"Read all: markdown_read {{ filename: {filename!r}, source: 'disk' }}.",
            open_hint,
        ],
    }
    if foreign:
        response["readOnly"] = True
    c.emit(response, args.pretty)
    return 0


# ---------------------------------------------------------------------------
# Section -> dict
# ---------------------------------------------------------------------------

def _serialize_section(s, *, include_state: bool) -> dict:
    out: dict = {
        "sectionNumber": s.section_number,
        "depth": s.depth,
        "title": s.title,
        "wordCount": count_words(s.body),
    }
    if include_state and s.state:
        out["state"] = s.state
        if s.seed is not None:
            out["seed"] = s.seed
        if s.session_id:
            out["sessionId"] = s.session_id
    return out
