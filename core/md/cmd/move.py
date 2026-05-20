"""markdown_move: reparent or reorder a section.

Same position keywords as insert: --before / --after / --under /
--top-level. The moved section's UUID stays the same (revisions and
dispatched session_id survive); only its position in the tree changes,
which means its sectionNumber and any descendants' sectionNumbers
shift.

Returns the standard changeReport + userSummary.
"""

from __future__ import annotations

import argparse

from . import _common as c
from ..ids import compute_section_number
from ..sections import (
    fetch_section,
    move_section as _move_section,
    position_for_insert,
)
from ..states import STATE_READONLY
from ..storage import open_db
from ..util import now


def register_parser(sub) -> None:
    p = sub.add_parser("move",
                       help="move a section to a new position (reparent or reorder)")
    p.add_argument("--filename", required=True)
    p.add_argument("--section-number", required=True,
                   help="sectionNumber of the section to move")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--before",
                     help="move just before this sectionNumber OR title "
                          "(same parent). Title-resolution is global by "
                          "default; pass --parent-section to scope.")
    grp.add_argument("--after",
                     help="move just after this sectionNumber OR title "
                          "(same parent). Title-resolution is global by "
                          "default; pass --parent-section to scope.")
    grp.add_argument("--under",
                     help="move as last child of this sectionNumber OR "
                          "title. Title-resolution is global by default; "
                          "pass --parent-section to scope.")
    grp.add_argument("--top-level", action="store_true",
                     help="move to end of top-level chain")
    p.add_argument("--parent-section",
                   help="when --before/--after/--under is a title, scope "
                        "the title search to the named section's subtree.")
    c.add_pretty(p)
    p.set_defaults(func=cmd_move)


def cmd_move(args: argparse.Namespace) -> int:
    resolved = c.resolve_workspace_or_die(args, require_writable=True)
    if resolved is None:
        return 1
    _, ws_dir, filename = resolved

    with open_db(ws_dir / "db.sqlite3") as conn:
        uuid = c.resolve_section_handle(
            conn, args.section_number, arg_name="section-number",
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

        position_kwargs: dict = {}
        parent_scope = args.parent_section
        if args.top_level:
            position_kwargs = {"at_top_level": True}
        else:
            for kw, val in (("before", args.before), ("after", args.after),
                            ("under", args.under)):
                if val is not None:
                    target_uuid = c.resolve_section_handle(
                        conn, val, parent_scope=parent_scope, arg_name=kw,
                    )
                    if target_uuid is None:
                        return 1
                    if target_uuid == uuid:
                        c.emit_error({
                            "error": "cannot move a section relative to itself",
                        })
                        return 2
                    position_kwargs = {kw: target_uuid}
                    break

        try:
            new_parent_id, new_prev_sibling_id = position_for_insert(
                conn, **position_kwargs,
            )
        except ValueError as e:
            c.emit_error({"error": str(e)})
            return 2

        if new_prev_sibling_id == uuid:
            new_prev_sibling_id = sec.prev_sibling_id

        before_snapshot = c.take_outline_snapshot(conn)

        try:
            _move_section(
                conn, uuid,
                new_parent_id=new_parent_id,
                new_prev_sibling_id=new_prev_sibling_id,
                now=now(),
            )
        except ValueError as e:
            c.emit_error({"error": str(e)})
            return 1

        after_snapshot = c.take_outline_snapshot(conn)
        report = c.compute_change_report(
            before_snapshot, after_snapshot, filename,
            conn=conn, by="primary",
        )

        new_addr = compute_section_number(conn, uuid)

    response = {
        "filename": filename,
        "sectionNumber": new_addr.display,
        "changeReport": report["changeReport"],
        "userSummary": report["userSummary"],
    }
    c.emit(response, args.pretty)
    return 0
