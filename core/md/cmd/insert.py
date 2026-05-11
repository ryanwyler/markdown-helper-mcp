"""markdown_insert: insert a new section at a specific position.

Position is one of:
  --before <sectionNumber>     : insert just before that section
  --after  <sectionNumber>     : insert just after that section
  --under  <sectionNumber>     : append as last child of that section
  --top-level                  : append to end of forest

Exactly one is required.

The new section can carry a title, seed, and/or content. State defaults
to the schema's initial state.

Modes (when content is provided):
  --mode body     : content is the section's body literally (default).
  --mode subtree  : content is parsed via CommonMark; if it contains
                    headings, the resulting tree becomes children of
                    the new section. Auto-split.

Returns the standard changeReport + userSummary.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import _common as c
from ..ids import compute_section_number
from ..parse import parse_markdown_to_subtree
from ..sections import (
    fetch_section,
    insert_section,
    position_for_insert,
    section_depth,
    splice_subtree,
)
from ..states import STATE_READONLY
from ..storage import open_db
from ..util import maybe_unescape_literal_newlines, now


def register_parser(sub) -> None:
    p = sub.add_parser("insert",
                       help="insert a new section at a specific position")
    p.add_argument("--filename", required=True)
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--before",
                     help="sectionNumber to insert just before (same parent)")
    grp.add_argument("--after",
                     help="sectionNumber to insert just after (same parent)")
    grp.add_argument("--under",
                     help="sectionNumber to insert as last child of")
    grp.add_argument("--top-level", action="store_true",
                     help="append as new top-level section (last in forest)")
    p.add_argument("--title",
                   help="heading text. Omit for headless preamble (top-level only).")
    p.add_argument("--seed",
                   help="contract describing what this section should cover")
    p.add_argument("--content", help="initial content (optional)")
    p.add_argument("--content-file", help="path to a file holding the content")
    p.add_argument("--mode", choices=["body", "subtree"], default="body",
                   help="body (default): content is the new section's body. "
                        "subtree: parse content; headings auto-split into descendants.")
    p.add_argument("--state",
                   help="initial state (default: schema's initial state)")
    p.add_argument("--by", help="writer identifier; defaults to 'primary'")
    c.add_pretty(p)
    p.set_defaults(func=cmd_insert)


def cmd_insert(args: argparse.Namespace) -> int:
    resolved = c.resolve_workspace_or_die(args, require_writable=True)
    if resolved is None:
        return 1
    _, ws_dir, filename = resolved

    content = args.content
    if args.content_file:
        try:
            content = Path(args.content_file).read_text(encoding="utf-8")
        except OSError as e:
            c.emit_error({"error": f"could not read content file: {e}"})
            return 2

    # Recover from agents that JSON-double-encoded the content (literal
    # `\n` instead of real newlines). Silent: only triggers when there
    # are zero real newlines AND at least one literal `\n`, so the
    # cases where it fires are unambiguous bugs and the agent doesn't
    # need to be told. See core/md/util.py::maybe_unescape_literal_newlines.
    content, _ = maybe_unescape_literal_newlines(content)

    title = args.title if args.title else None

    with open_db(ws_dir / "db.sqlite3") as conn:
        schema = c.schema_or_die(conn)

        # Validate state if provided.
        if not c.validate_state_or_die(schema, args.state):
            return 2

        state = args.state if args.state is not None else schema.initial_state()

        # Resolve target position.
        target_uuid: str | None = None
        position_kwargs: dict = {}

        if args.top_level:
            position_kwargs = {"at_top_level": True}
        elif args.before:
            target_uuid = c.resolve_section_or_die(conn, args.before)
            if target_uuid is None:
                return 1
            position_kwargs = {"before": target_uuid}
        elif args.after:
            target_uuid = c.resolve_section_or_die(conn, args.after)
            if target_uuid is None:
                return 1
            position_kwargs = {"after": target_uuid}
        elif args.under:
            target_uuid = c.resolve_section_or_die(conn, args.under)
            if target_uuid is None:
                return 1
            position_kwargs = {"under": target_uuid}

        # Refuse if the target is a readonly section (foreign).
        if target_uuid is not None:
            target_sec = fetch_section(conn, target_uuid)
            if target_sec is not None and target_sec.state == STATE_READONLY:
                c.emit_error({
                    "error": "cannot insert relative to a read-only section",
                    "readOnly": True,
                    "hint": "Foreign-doc sections refuse all mutations.",
                })
                return 1

        try:
            parent_id, prev_sibling_id = position_for_insert(conn, **position_kwargs)
        except ValueError as e:
            c.emit_error({"error": str(e)})
            return 2

        if title is None and parent_id is not None:
            c.emit_error({
                "error": "only top-level sections may have title=None (headless preamble)",
                "hint": "Provide --title or insert at top-level / before/after a top-level section.",
            })
            return 2

        before_snapshot = c.take_outline_snapshot(conn)

        if args.mode == "subtree" and content:
            # Subtree: insert the section first (with body=None), then
            # splice the parsed children into it.
            new_section = insert_section(
                conn,
                parent_id=parent_id,
                prev_sibling_id=prev_sibling_id,
                title=title,
                seed=args.seed,
                content=None,
                state=state,
                updated_by=args.by or "primary",
                now=now(),
            )
            depth = section_depth(conn, new_section.id)
            body, child_specs = parse_markdown_to_subtree(content, target_depth=depth)
            splice_subtree(
                conn, new_section.id,
                new_body=body if body else None,
                new_title=None,
                children_specs=child_specs,
                state=state,
                updated_by=args.by or "primary",
                session_id=None,
                now=now(),
            )
        else:
            new_section = insert_section(
                conn,
                parent_id=parent_id,
                prev_sibling_id=prev_sibling_id,
                title=title,
                seed=args.seed,
                content=content,
                state=state,
                updated_by=args.by or "primary",
                now=now(),
            )

        new_addr = compute_section_number(conn, new_section.id)
        after_snapshot = c.take_outline_snapshot(conn)
        report = c.compute_change_report(before_snapshot, after_snapshot, filename)

    response: dict = {
        "filename": filename,
        "sectionNumber": new_addr.display,
        "changeReport": report["changeReport"],
        "userSummary": report["userSummary"],
        "outline": report["outline"],
    }
    c.emit(response, args.pretty)
    return 0
