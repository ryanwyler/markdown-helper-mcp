"""markdown_open: ingest an existing markdown file. Non-destructive.

THREE BRANCHES
==============

1. Fresh open (no workspace exists for this file)
   - Parse the disk file via mistletoe.
   - Insert every section into the workspace at state=`loaded` (a
     tool-managed pseudo-state).
   - For foreign files, state=`readonly` instead.
   - Apply the agent's `states` param if provided; default schema
     [pending, done] otherwise. Schema persists to meta.

2. Resync of an already-open workspace, content matches disk
   - Walk parsed disk sections vs workspace sections by render order.
   - For every section pair where content+title matches, leave the
     workspace untouched (state preserved).
   - Response: `divergence: []`, no nudge.

3. Resync of an already-open workspace, content diverges
   - Same walk as branch 2, but flag every section where disk content
     differs from workspace content as a divergence entry.
   - **Do NOT mutate the workspace.** The agent decides via:
       - markdown_save (push workspace to disk, overwrites disk)
       - markdown_open { reset: true } (discard workspace, adopt disk)
       - markdown_get / read (inspect each, decide piece by piece)
   - Response: `divergence: [{sectionNumber, diskHash, workspaceHash,
     recommendedAction}]`, plus actionable nextSteps.

`reset: true` flag (param) bypasses reconcile entirely: wipes workspace
and rebuilds from disk fresh, loaded pseudo-state. Use when the agent
explicitly wants disk to win.

The schema and meta survive reconcile -- never wiped, never rebuilt
unless reset=true.
"""

from __future__ import annotations

import argparse
import hashlib
import json

from . import _common as c
from ..parse import parse_markdown
from ..revisions import record_revision
from ..sections import (
    append_child,
    fetch_all_sections_in_order,
)
from ..states import STATE_LOADED, STATE_READONLY, Schema
from ..storage import (
    file_db_path,
    file_workspace_exists,
    find_helper_root,
    get_schema,
    meta_set,
    open_db,
    remove_touched_entry,
    resolve_filename,
    set_schema,
)
from ..trailer import apply_trailer, parse_trailer, strip_trailer
from ..util import now


def register_parser(sub) -> None:
    p = sub.add_parser("open",
                       help="open an existing markdown file (non-destructive)")
    p.add_argument("--filename", required=True)
    p.add_argument("--reset", action="store_true",
                   help="discard any existing workspace state and reload "
                        "from disk. Use only when you explicitly want disk "
                        "to win over the in-memory workspace.")
    p.add_argument("--states-json",
                   help="JSON for the state schema; only honored on FRESH "
                        "open (no existing workspace). Reopening a workspace "
                        "preserves its declared schema.")
    c.add_pretty(p)
    p.set_defaults(func=cmd_open)


def cmd_open(args: argparse.Namespace) -> int:
    if not args.filename:
        c.emit_error({"error": "filename is required"})
        return 2

    try:
        resolved = resolve_filename(args.filename)
    except ValueError as e:
        c.emit_error({"error": str(e)})
        return 2
    filename = resolved.canonical
    src = resolved.disk_path
    if not src.exists():
        c.emit_error({
            "error": f"file {filename!r} does not exist on disk "
                     f"(looked at {src})",
            "hint": "Use markdown_create to scaffold a new file (local only).",
        })
        return 1

    raw_text = src.read_text(encoding="utf-8")
    # Strip any trailer block before parsing the markdown body. The
    # trailer is a sidecar of per-section state -- it's not part of
    # the prose. Even if the file has no trailer, strip_trailer is a
    # no-op and returns (text, None).
    text, trailer_json = strip_trailer(raw_text)
    parsed_trailer = parse_trailer(trailer_json) if trailer_json else None
    parsed_sections = parse_markdown(text)

    root = find_helper_root()
    workspace_key = resolved.workspace_key
    is_resync = file_workspace_exists(root, workspace_key)
    db_path = file_db_path(root, workspace_key)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Resolve agent-provided schema (only honored on fresh open or reset).
    requested_schema: Schema | None = None
    if args.states_json:
        if is_resync and not args.reset:
            c.emit_error({
                "error": "states param ignored on reopen of an existing workspace; "
                         "the workspace's declared schema is preserved",
                "hint": "Pass reset=true to wipe the workspace and apply a new schema.",
            })
            return 2
        try:
            states_raw = json.loads(args.states_json)
        except json.JSONDecodeError as e:
            c.emit_error({"error": f"invalid --states-json: {e}"})
            return 2
        try:
            requested_schema = Schema.from_param(states_raw)
        except ValueError as e:
            c.emit_error({"error": str(e)})
            return 2

    # ----- Branch dispatch -----
    if not is_resync:
        return _fresh_open(
            db_path, parsed_sections, filename, args, resolved,
            requested_schema=requested_schema,
            root=root, workspace_key=workspace_key,
            parsed_trailer=parsed_trailer,
        )

    if args.reset:
        return _reset_open(
            db_path, parsed_sections, filename, args, resolved,
            requested_schema=requested_schema,
            parsed_trailer=parsed_trailer,
        )

    # Reconcile path intentionally does NOT apply the trailer -- the
    # workspace's state is the source of truth on reopen. Re-applying
    # the trailer would clobber in-flight work.
    return _reconcile_open(
        db_path, parsed_sections, filename, args, resolved,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_section(title: str | None, body: str | None) -> str:
    h = hashlib.sha256()
    h.update((title or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((body or "").encode("utf-8"))
    return h.hexdigest()[:16]


def _ingest_disk_into_workspace(
    conn, parsed_sections, *, foreign: bool,
) -> None:
    """Insert every parsed section into the workspace as `loaded`
    (or `readonly` for foreign), in source order.
    """
    initial_state = STATE_READONLY if foreign else STATE_LOADED
    stack: list[tuple[int, str]] = []
    for ps in parsed_sections:
        if ps.level == 0:
            stack = []
            inserted = append_child(
                conn, parent_id=None,
                title=None, seed=None,
                content=ps.body if ps.body else None,
                state=initial_state,
                updated_by="open",
                now=now(),
            )
        else:
            while stack and stack[-1][0] >= ps.level:
                stack.pop()
            parent_id = stack[-1][1] if stack else None
            inserted = append_child(
                conn, parent_id=parent_id,
                title=ps.title, seed=None,
                content=ps.body if ps.body else None,
                state=initial_state,
                updated_by="open",
                now=now(),
            )
            stack.append((ps.level, inserted.id))
        record_revision(
            conn, section_id=inserted.id,
            content=ps.body or None, state=initial_state,
            notes="ingested via open" if ps.body else "ingested via open (empty section)",
            by="open",
        )


def _build_response_skeleton(
    conn, *, filename: str, foreign: bool, source: str,
) -> dict:
    """Common response fields populated from the workspace."""
    sections_out = [
        c.summarize_section(conn, s) for s in fetch_all_sections_in_order(conn)
    ]
    response: dict = {
        "filename": filename,
        "source": source,
        "fileOnDisk": True,
        "sectionCount": len(sections_out),
        "sections": sections_out,
        "schema": get_schema(conn).to_response(),
    }
    if foreign:
        response["readOnly"] = True
    return response


def _foreign_next_steps(filename: str) -> list[str]:
    return [
        f"Foreign (cross-project) file opened READ-ONLY. Use "
        f"markdown_outline / markdown_read / markdown_get to inspect. "
        f"In subsequent calls, use filename: {filename!r} (the realpath).",
        f"To EDIT: copy into your current project first with "
        f"markdown_save {{ filename: {filename!r}, asFilename: '<cwd-rel-path>.md' }}.",
    ]


# ---------------------------------------------------------------------------
# Branch 1: fresh open
# ---------------------------------------------------------------------------

def _fresh_open(
    db_path, parsed_sections, filename, args, resolved, *,
    requested_schema: Schema | None,
    root, workspace_key,
    parsed_trailer: dict | None = None,
) -> int:
    foreign = resolved.is_foreign

    # Schema priority order:
    #   1. agent-provided --states-json (requested_schema)
    #   2. schema embedded in the trailer (if present)
    #   3. default ([pending, done])
    schema: Schema
    if requested_schema is not None:
        schema = requested_schema
    elif parsed_trailer and parsed_trailer.get("schema"):
        try:
            schema = Schema.from_param(parsed_trailer["schema"])
        except ValueError:
            schema = Schema.default()
    else:
        schema = Schema.default()

    orphans: list[dict] = []
    with open_db(db_path) as conn:
        meta_set(conn, "createdAt", str(now()))
        meta_set(conn, c.META_FOREIGN, "1" if foreign else "0")
        if foreign:
            meta_set(conn, "foreignSource", str(resolved.disk_path))
        set_schema(conn, schema)
        _ingest_disk_into_workspace(conn, parsed_sections, foreign=foreign)
        # Apply trailer overrides (state/seed/tags + archived subtrees).
        # Foreign workspaces still benefit -- the trailer round-trips
        # information that's gone from the visible doc.
        if parsed_trailer is not None:
            orphans = apply_trailer(
                conn, parsed_trailer, schema=schema, now_ts=now(),
            )
        # File on disk IS the source of truth right now.
        meta_set(conn, "last_saved_at", str(now()))

        response = _build_response_skeleton(
            conn, filename=filename, foreign=foreign, source="fresh",
        )
        response["reloaded"] = False
        response["fromDisk"] = True
        if orphans:
            response["trailerOrphans"] = orphans

    # If we were tracking this file as touch-only, drop that marker --
    # the workspace promotion supersedes it.
    remove_touched_entry(root, workspace_key)

    if foreign:
        response["nextSteps"] = _foreign_next_steps(filename)
    else:
        response["nextSteps"] = [
            f"Sections came in at state {STATE_LOADED!r} (tool-managed). Writing "
            f"any section transitions it into your declared schema "
            f"({list(schema.names())}). No force needed for the first write.",
        ]

    c.emit(response, args.pretty)
    return 0


# ---------------------------------------------------------------------------
# Branch 2: reset open (explicit nuke + reload)
# ---------------------------------------------------------------------------

def _reset_open(
    db_path, parsed_sections, filename, args, resolved, *,
    requested_schema: Schema | None,
    parsed_trailer: dict | None = None,
) -> int:
    foreign = resolved.is_foreign

    # Schema for the reset workspace. If the agent explicitly passed
    # one, that wins. Otherwise honor the trailer-embedded schema.
    # Otherwise keep whatever the existing workspace had.
    trailer_schema: Schema | None = None
    if parsed_trailer and parsed_trailer.get("schema"):
        try:
            trailer_schema = Schema.from_param(parsed_trailer["schema"])
        except ValueError:
            trailer_schema = None

    orphans: list[dict] = []
    with open_db(db_path) as conn:
        # Wipe sections + revisions + assistants. Keep meta (createdAt,
        # foreign flag) BUT honor a new schema if the agent passed one.
        conn.execute("DELETE FROM revisions")
        conn.execute("DELETE FROM assistants")
        conn.execute("DELETE FROM sections")
        if requested_schema is not None:
            set_schema(conn, requested_schema)
        elif trailer_schema is not None:
            set_schema(conn, trailer_schema)
        meta_set(conn, c.META_FOREIGN, "1" if foreign else "0")
        if foreign:
            meta_set(conn, "foreignSource", str(resolved.disk_path))

        _ingest_disk_into_workspace(conn, parsed_sections, foreign=foreign)
        # Apply trailer same as fresh open.
        schema_for_trailer = get_schema(conn)
        if parsed_trailer is not None:
            orphans = apply_trailer(
                conn, parsed_trailer, schema=schema_for_trailer, now_ts=now(),
            )
        meta_set(conn, "last_saved_at", str(now()))

        schema = get_schema(conn)
        response = _build_response_skeleton(
            conn, filename=filename, foreign=foreign, source="reset",
        )
        response["reloaded"] = True
        response["fromDisk"] = True
        if orphans:
            response["trailerOrphans"] = orphans

    if foreign:
        response["nextSteps"] = _foreign_next_steps(filename)
    else:
        response["nextSteps"] = [
            f"Workspace was reset. Sections came in at {STATE_LOADED!r}. "
            f"Writing any section transitions it into your schema "
            f"({list(schema.names())}).",
        ]

    c.emit(response, args.pretty)
    return 0


# ---------------------------------------------------------------------------
# Branch 3: reconcile open (non-destructive)
# ---------------------------------------------------------------------------

def _reconcile_open(
    db_path, parsed_sections, filename, args, resolved,
) -> int:
    """Reopen a workspace with content matching disk -> preserve all
    state. If content diverges -> emit divergence report, do NOT mutate.

    Strategy: walk parsed_sections (disk render order) alongside the
    workspace's render order. For each pair (by position):
      - if titles match AND bodies match -> no divergence on this slot.
      - otherwise -> divergence entry.

    If lengths differ we emit an "unmatched" divergence for the surplus
    on either side.

    No DB writes happen in this path. The agent resolves divergence by
    one of: save (push), open reset (pull), or get/read+set per section.
    """
    foreign = resolved.is_foreign

    with open_db(db_path) as conn:
        ws_sections = fetch_all_sections_in_order(conn)

        divergence: list[dict] = []
        n = max(len(ws_sections), len(parsed_sections))
        for i in range(n):
            ws = ws_sections[i] if i < len(ws_sections) else None
            ds = parsed_sections[i] if i < len(parsed_sections) else None

            if ws is None and ds is not None:
                divergence.append({
                    "sectionNumber": f"(disk index {i})",
                    "diskHash": _hash_section(ds.title, ds.body),
                    "workspaceHash": None,
                    "kind": "extra-on-disk",
                    "title": ds.title,
                    "recommendedAction":
                        "Disk has a section the workspace doesn't. "
                        "open reset:true to adopt disk.",
                })
                continue
            if ws is not None and ds is None:
                from ..ids import compute_section_number
                addr = compute_section_number(conn, ws.id).display
                divergence.append({
                    "sectionNumber": addr,
                    "diskHash": None,
                    "workspaceHash": _hash_section(ws.title, ws.content),
                    "kind": "extra-in-workspace",
                    "title": ws.title,
                    "recommendedAction":
                        "Workspace has a section disk doesn't. "
                        "save to push, or open reset:true to drop it.",
                })
                continue

            assert ws is not None and ds is not None  # for the type checker

            ds_title = ds.title
            ds_body = ds.body or None
            # Normalize trailing newlines to compare meaningfully.
            ws_body = (ws.content or "").rstrip("\n")
            ds_body_norm = (ds_body or "").rstrip("\n")
            if ws.title == ds_title and ws_body == ds_body_norm:
                continue  # no divergence

            from ..ids import compute_section_number
            addr = compute_section_number(conn, ws.id).display
            divergence.append({
                "sectionNumber": addr,
                "diskHash": _hash_section(ds_title, ds_body),
                "workspaceHash": _hash_section(ws.title, ws.content),
                "kind": ("content-mismatch" if ws.title == ds_title
                         else "title-and-content-mismatch"),
                "title": ws.title,
                "recommendedAction":
                    "Inspect both: markdown_get for workspace, markdown_read "
                    "source:disk for the disk version. Decide which wins.",
            })

        response = _build_response_skeleton(
            conn, filename=filename, foreign=foreign, source="reconcile",
        )
        response["reloaded"] = False
        response["divergence"] = divergence

    if not divergence:
        response["nextSteps"] = [
            "Workspace and disk match. Continue editing as normal.",
        ]
    else:
        response["nextSteps"] = [
            f"{len(divergence)} section(s) diverge between workspace and disk.",
            "Workspace has NOT been mutated. You have three options:",
            f"  1. markdown_save {{ filename: {filename!r} }}  -- push workspace to disk (overwrites disk).",
            f"  2. markdown_open {{ filename: {filename!r}, reset: true }}  -- discard workspace and adopt disk.",
            f"  3. Inspect each divergent section: markdown_get / "
            f"markdown_read source:'disk' to compare, then decide.",
        ]

    c.emit(response, args.pretty)
    return 0
