"""markdown_review: transition a section to a chosen schema state.

Schema-aware. Two modes:

  --to-state <name>           : transition the section to <name> (must
                                be in declared schema). Optional --notes.
  --action accept|reject      : shortcut.
                                accept -> schema's terminal state.
                                reject -> schema's needsAttention state.
                                Errors out with helpful message if the
                                shortcut isn't supported by the schema.

Notes are recorded in the revision history regardless of mode -- they
travel with the section across reopens (in revision history) so the
next writer reads them via markdown_get.
"""

from __future__ import annotations

import argparse

from . import _common as c
from ..revisions import record_revision
from ..sections import fetch_section, update_section_content
from ..states import STATE_READONLY, validate_state
from ..storage import open_db
from ..util import now


def register_parser(sub) -> None:
    p = sub.add_parser("review",
                       help="transition a section to a schema state (with optional notes)")
    p.add_argument("--filename", required=True)
    p.add_argument("--section-number", required=True)
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--to-state",
                     help="explicit target state (must be in declared schema)")
    grp.add_argument("--action", choices=["accept", "reject"],
                     help="shortcut: accept -> schema's terminal state; "
                          "reject -> schema's needsAttention state.")
    p.add_argument("--notes", help="reviewer notes; recorded in history")
    c.add_pretty(p)
    p.set_defaults(func=cmd_review)


def cmd_review(args: argparse.Namespace) -> int:
    resolved = c.resolve_workspace_or_die(args, require_writable=True)
    if resolved is None:
        return 1
    _, ws_dir, filename = resolved

    with open_db(ws_dir / "db.sqlite3") as conn:
        schema = c.schema_or_die(conn)

        # Resolve target state.
        if args.to_state is not None:
            try:
                validate_state(schema, args.to_state)
            except ValueError as e:
                c.emit_error({"error": str(e), "validStates": list(schema.names())})
                return 2
            target_state = args.to_state
        else:
            # Shortcut.
            if args.action == "accept":
                term = schema.terminal_state()
                if term is None:
                    c.emit_error({
                        "error": "no terminal state declared in this doc's schema; "
                                 "specify toState explicitly",
                        "validStates": list(schema.names()),
                    })
                    return 2
                target_state = term
            else:  # reject
                needs = schema.needs_attention_state()
                if needs is None:
                    c.emit_error({
                        "error": "no needsAttention state declared in this doc's "
                                 "schema; specify toState explicitly to use a reject "
                                 "shortcut, or declare a needsAttention state.",
                        "validStates": list(schema.names()),
                    })
                    return 2
                target_state = needs
                if not args.notes:
                    c.emit_error({"error": "--notes is required when rejecting"})
                    return 2

        uuid = c.resolve_section_or_die(conn, args.section_number)
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

        update_section_content(
            conn, uuid,
            content=sec.content,
            state=target_state,
            updated_by="primary",
            session_id=None,
            now=now(),
        )
        record_revision(
            conn, section_id=uuid, content=sec.content, state=target_state,
            notes=args.notes, by="primary",
        )

        sec_after = fetch_section(conn, uuid)
        response = c.summarize_section(conn, sec_after) if sec_after else {}

    response["filename"] = filename
    response["toState"] = target_state
    if args.action:
        response["action"] = args.action
    if args.notes:
        response["notes"] = args.notes
    c.emit(response, args.pretty)
    return 0
