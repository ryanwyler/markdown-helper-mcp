"""markdown_set: replace a section's content (and optionally rename it).

Two call shapes:

  1. Single-section: --section-number --content [--state ...]
  2. Batch:           --writes-json '[{...}, {...}]'

Each write entry takes:
  sectionNumber  : (required)
  content        : (required)
  state          : (optional; must be in declared schema)
  title          : (optional; rename, "" to clear)
  seed           : (optional)
  mode           : 'body' (default) | 'subtree'
                   'subtree' parses content via CommonMark; if there
                   are headings, the resulting tree is spliced in,
                   replacing target's existing descendants. Auto-split.

State machine:
  - readonly section -> always refuses (foreign doc).
  - loaded section -> first write transitions to schema's initial
    state (or whatever the agent passes via state).
  - terminal section -> requires force=true.
  - any other declared state -> writes freely.

Batch writes are wrapped in a single SQLite transaction. All-or-
nothing on validation failure (any entry's failure rolls back the
whole batch). The standard changeReport is built from a snapshot of
the outline taken before the batch started.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import _common as c
from ..parse import parse_markdown_to_subtree
from ..revisions import record_revision
from ..sections import (
    fetch_section,
    section_depth,
    splice_subtree,
    update_section_content,
)
from ..states import (
    PSEUDO_STATES,
    STATE_LOADED,
    auto_transition,
    needs_force,
    validate_state,
)
from ..storage import open_db
from ..util import maybe_unescape_literal_newlines, normalize_tags, now


_TAGS_UNSET = object()


def register_parser(sub) -> None:
    p = sub.add_parser("set",
                       help="write/replace a section's content (and optionally rename)")
    p.add_argument("--filename", required=True)
    # Single-section args
    p.add_argument("--section-number",
                   help="sectionNumber OR title.")
    p.add_argument("--parent-section",
                   help="scope title-resolution to the named section's "
                        "subtree (use when --section-number is a title "
                        "that matches multiple sections).")
    p.add_argument("--content", help="markdown body for the section")
    p.add_argument("--content-file", help="path to a file holding the content")
    p.add_argument("--title",
                   help="rename heading. Omit to leave unchanged. Empty string -> headless.")
    p.add_argument("--seed", help="update the section's seed.")
    p.add_argument("--state", help="post-write state (must be in declared schema).")
    p.add_argument("--tags",
                   help="replace the section's tag set. JSON array of "
                        "lowercase-normalized identifier strings, e.g. "
                        "'[\"billing\",\"v1\"]'. Pass '[]' to clear. "
                        "Whitespace within a tag is rejected; tags are "
                        "identifiers, not free-form notes.")
    p.add_argument("--mode", choices=["body", "subtree"], default="body",
                   help="body: replace section body literally. "
                        "subtree: parse content via CommonMark; headings "
                        "auto-split into descendant sections.")
    p.add_argument("--by", help="writer identifier; defaults to 'primary'")
    p.add_argument("--session-id", help="opencode/claude session id")
    p.add_argument("--force", action="store_true",
                   help="overwrite a terminal-state section")
    # Batch arg
    p.add_argument("--writes-json",
                   help="JSON array of write entries for batch mode. "
                        "Each entry: {sectionNumber, content, state?, "
                        "title?, seed?, mode?}. Wrapped in a single "
                        "transaction (all-or-nothing).")
    c.add_pretty(p)
    p.set_defaults(func=cmd_set)


def cmd_set(args: argparse.Namespace) -> int:
    resolved = c.resolve_workspace_or_die(args, require_writable=True)
    if resolved is None:
        return 1
    _, ws_dir, filename = resolved

    # Resolve write entries.
    entries: list[dict[str, Any]] = []
    if args.writes_json:
        try:
            parsed = json.loads(args.writes_json)
        except json.JSONDecodeError as e:
            c.emit_error({"error": f"invalid --writes-json: {e}"})
            return 2
        if not isinstance(parsed, list):
            c.emit_error({"error": "--writes-json must be a JSON array"})
            return 2
        entries = parsed
    else:
        if not args.section_number:
            c.emit_error({"error": "sectionNumber is required (or use writes:[])"})
            return 2
        if args.content is None and not args.content_file:
            c.emit_error({"error": "either content or contentFile is required"})
            return 2
        content = args.content
        if args.content_file:
            try:
                content = Path(args.content_file).read_text(encoding="utf-8")
            except OSError as e:
                c.emit_error({"error": f"could not read content file: {e}"})
                return 2
        entry: dict[str, Any] = {
            "sectionNumber": args.section_number,
            "content": content,
            "mode": args.mode,
        }
        if args.parent_section is not None:
            entry["parentSection"] = args.parent_section
        if args.title is not None:
            entry["title"] = args.title
        if args.seed is not None:
            entry["seed"] = args.seed
        if args.state is not None:
            entry["state"] = args.state
        if args.tags is not None:
            try:
                entry["tags"] = json.loads(args.tags)
            except json.JSONDecodeError as e:
                c.emit_error({"error": f"--tags must be a JSON array: {e}"})
                return 2
        entries = [entry]

    if not entries:
        c.emit_error({"error": "no write entries provided"})
        return 2

    by = args.by or "primary"
    session_id = args.session_id
    force = bool(args.force)

    with open_db(ws_dir / "db.sqlite3") as conn:
        schema = c.schema_or_die(conn)
        before_snapshot = c.take_outline_snapshot(conn)

        try:
            _apply_writes(conn, entries, schema=schema, by=by,
                          session_id=session_id, force=force)
        except _SetError as e:
            c.emit_error(e.payload)
            return e.code

        after_snapshot = c.take_outline_snapshot(conn)
        report = c.compute_change_report(
            before_snapshot, after_snapshot, filename,
            conn=conn, by=by,
        )

    response: dict[str, Any] = {
        "filename": filename,
        "writeCount": len(entries),
        "changeReport": report["changeReport"],
        "userSummary": report["userSummary"],
    }
    c.emit(response, args.pretty)
    return 0


# ---------------------------------------------------------------------------
# Internal apply loop
# ---------------------------------------------------------------------------

class _SetError(Exception):
    """Raised inside _apply_writes to abort the batch with a payload."""
    def __init__(self, payload: dict[str, Any], code: int = 1):
        self.payload = payload
        self.code = code
        super().__init__(payload.get("error", "set failed"))


def _apply_writes(
    conn,
    entries: list[dict[str, Any]],
    *,
    schema,
    by: str,
    session_id: str | None,
    force: bool,
) -> None:
    """Apply each entry to the workspace. Validates everything FIRST
    (resolving section numbers, checking states, checking force gates)
    so a single bad entry kills the whole batch before any DB write.

    NOTE: we resolve sectionNumbers UPFRONT against the pre-batch
    outline. If an earlier write inserts new sections via auto-split,
    later entries' sectionNumbers reference the pre-batch numbering --
    intentionally positional, not address-tracking. Documented in
    DESIGN.md E.4.
    """
    # PASS 1: validate every entry, resolve uuids, capture sections.
    plans: list[dict[str, Any]] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise _SetError({"error": f"writes[{i}]: must be an object"}, code=2)

        sn = entry.get("sectionNumber")
        content = entry.get("content")
        if not sn:
            raise _SetError({"error": f"writes[{i}]: sectionNumber is required"}, code=2)
        if content is None:
            raise _SetError({"error": f"writes[{i}]: content is required"}, code=2)
        # Recover from agents that JSON-double-encoded the content
        # (literal `\n` instead of real newlines). Silent recovery -- the
        # heuristic only triggers on unambiguous bugs (zero real newlines
        # AND at least one literal `\n`).
        content, _ = maybe_unescape_literal_newlines(content)

        mode = entry.get("mode", "body")
        if mode not in ("body", "subtree"):
            raise _SetError({
                "error": f"writes[{i}]: mode must be 'body' or 'subtree' (got {mode!r})",
            }, code=2)

        # Resolve the section. Accept sectionNumber form ("A.1.2") OR
        # exact title -- same contract as the single-section path and
        # markdown_insert/move position targets. The per-entry
        # `parentSection` narrows title-resolution scope when a title
        # is ambiguous (matches multiple sections); pass an ancestor's
        # sectionNumber.
        from ..ids import resolve_section_number, compute_section_number
        parent_scope = entry.get("parentSection")
        uuid = None
        try:
            uuid = resolve_section_number(conn, sn)
        except ValueError:
            # Not a valid section-number grammar -- try title.
            pass
        if uuid is None:
            # Title search. Optionally scope to parent's subtree.
            if parent_scope:
                # Accept parentSection as number OR title (R1: titles
                # are stable; agents type titles).
                scope_uuid = None
                try:
                    scope_uuid = resolve_section_number(conn, parent_scope)
                except ValueError:
                    pass
                if scope_uuid is None:
                    scope_rows = conn.execute(
                        "SELECT id FROM sections WHERE title = ?",
                        (parent_scope,),
                    ).fetchall()
                    if len(scope_rows) == 1:
                        scope_uuid = scope_rows[0]["id"]
                    elif len(scope_rows) > 1:
                        raise _SetError({
                            "error": f"writes[{i}]: parentSection "
                                     f"{parent_scope!r} matches "
                                     f"{len(scope_rows)} sections; ambiguous",
                            "hint": "Use a sectionNumber for parentSection, "
                                    "or pick an unambiguous ancestor title.",
                        }, code=2)
                if scope_uuid is None:
                    raise _SetError({
                        "error": f"writes[{i}]: parentSection "
                                 f"{parent_scope!r} not found",
                    }, code=2)
                from ._common import _find_title_in_subtree
                match_uuids = _find_title_in_subtree(conn, scope_uuid, sn)
            else:
                rows = conn.execute(
                    "SELECT id FROM sections WHERE title = ?", (sn,),
                ).fetchall()
                match_uuids = [r["id"] for r in rows]
            if len(match_uuids) == 1:
                uuid = match_uuids[0]
            elif len(match_uuids) > 1:
                cands = []
                for uid in match_uuids:
                    addr = compute_section_number(conn, uid)
                    cands.append(addr.display)
                raise _SetError({
                    "error": f"writes[{i}]: title {sn!r} matches "
                             f"{len(match_uuids)} sections; ambiguous",
                    "candidates": cands,
                    "hint": "Pass an unambiguous title, a sectionNumber, "
                            "or add `parentSection: <ancestor>` to the "
                            "entry to narrow the search.",
                }, code=2)
        if uuid is None:
            raise _SetError({
                "error": f"writes[{i}]: {sn!r} is neither a sectionNumber "
                         f"nor an exact title match",
                "hint": "Use markdown_outline to see current titles and numbers.",
            }, code=1)
        sec = fetch_section(conn, uuid)
        if sec is None:
            raise _SetError({"error": f"writes[{i}]: section row missing for {sn!r}"}, code=1)

        # Refuse readonly sections (foreign).
        if sec.state == "readonly":
            raise _SetError({
                "error": f"writes[{i}]: section {sn!r} is read-only",
                "readOnly": True,
                "hint": "Use markdown_save asFilename to copy this doc into "
                        "the current project, then edit the local copy.",
            }, code=1)

        # Validate explicit state (if provided) against schema.
        explicit_state = entry.get("state")
        if explicit_state is not None:
            try:
                validate_state(schema, explicit_state)
            except ValueError as e:
                raise _SetError({
                    "error": f"writes[{i}]: {e}",
                    "validStates": list(schema.names()),
                }, code=2)

        # Normalize tags. Use sentinel to distinguish "not given"
        # (leave as-is) from "explicitly empty list" (clear).
        tags_val: object = _TAGS_UNSET
        if "tags" in entry:
            try:
                tags_val = normalize_tags(entry["tags"])
            except ValueError as e:
                raise _SetError({"error": f"writes[{i}]: {e}"}, code=2)

        # Force gate: terminal -> needs force.
        if needs_force(sec.state, schema) and not force:
            raise _SetError({
                **c.force_required_error(sec.state, schema),
                "writeIndex": i,
                "sectionNumber": sn,
            }, code=1)

        # Compute resulting state for this entry.
        if explicit_state is not None:
            new_state = explicit_state
        else:
            new_state = auto_transition(sec.state, schema)

        plans.append({
            "uuid": uuid,
            "sec": sec,
            "content": content,
            "title": entry.get("title"),  # None means leave alone
            "seed": entry.get("seed"),
            "state": new_state,
            "mode": mode,
            "tags": tags_val,  # _TAGS_UNSET means leave alone
        })

    # PASS 2: apply each plan.
    for plan in plans:
        sec = plan["sec"]
        uuid = plan["uuid"]
        content = plan["content"]
        title = plan["title"]
        seed = plan["seed"]
        state = plan["state"]
        mode = plan["mode"]

        # Mark any running dispatch jobs done if section was dispatched.
        # (Generic: any working state behaves the same. We mark assistants
        # rows terminal whenever the section had ANY working role.)
        if schema.get(sec.state) and schema.get(sec.state).working:
            conn.execute(
                """UPDATE assistants SET state='done', ended_at=?
                   WHERE section_id=? AND state IN ('queued', 'running')""",
                (now(), uuid),
            )

        if mode == "subtree":
            depth = section_depth(conn, uuid)
            body, child_specs = parse_markdown_to_subtree(
                content, target_depth=depth,
            )
            splice_subtree(
                conn, uuid,
                new_body=body if body else None,
                new_title=title,
                children_specs=child_specs,
                state=state,
                updated_by=by,
                session_id=session_id,
                now=now(),
            )
        else:
            kwargs = dict(
                content=content,
                title=title,
                seed=seed,
                state=state,
                updated_by=by,
                session_id=session_id,
                now=now(),
            )
            if plan["tags"] is not _TAGS_UNSET:
                kwargs["tags"] = plan["tags"]
            update_section_content(conn, uuid, **kwargs)
        record_revision(
            conn, section_id=uuid, content=content, state=state,
            notes=None, by=by,
        )
