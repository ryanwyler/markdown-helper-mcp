"""markdown_dispatch: spawn (or kill) an opencode sub-agent on a section.

Schema-aware: the section's state on dispatch is determined by the
declared schema. Default target = the schema's working state if
declared, otherwise the section is left in its current state and a
warning is emitted.

Spawn (default):
  --filename --section-number
  Section's current state is checked: terminal -> needs --force.
  readonly -> always refused.
  Otherwise: section state -> resultState (or schema's working state).

Kill:
  --filename --section-number --kill
  Kills any running dispatch on the section, resets section state to
  the schema's initial state.

Session reuse: if the section has a captured session_id, the new
dispatch resumes that session via opencode --session.
"""

from __future__ import annotations

import argparse
from typing import Any

from . import _common as c
from ..dispatch import (
    build_prompt,
    dispatch_log_path,
    gen_job_id,
    kill_running_dispatches_for_section,
    reconcile_all_dispatched,
    spawn_opencode_dispatch,
)
from ..ids import compute_section_number
from ..sections import fetch_section
from ..states import (
    STATE_READONLY,
    needs_force,
    validate_state,
)
from ..storage import cwd_path, find_project_root, open_db
from ..util import now


def register_parser(sub) -> None:
    p = sub.add_parser("dispatch",
                       help="spawn (or kill) an opencode sub-agent on a section")
    p.add_argument("--filename", required=True)
    p.add_argument("--section-number", required=True,
                   help="sectionNumber OR title.")
    p.add_argument("--parent-section",
                   help="scope title-resolution to the named section's "
                        "subtree (use when --section-number is a title "
                        "that matches multiple sections).")
    p.add_argument("--kill", action="store_true",
                   help="kill any running dispatch; resets section to schema's initial state")
    p.add_argument("--agent", default="build",
                   help="opencode agent profile (default: build)")
    p.add_argument("--model", help="model override (provider/model)")
    p.add_argument("--session-id",
                   help="explicit session id to continue (overrides reuse-session)")
    p.add_argument("--reuse-session", dest="reuse_session",
                   action="store_true", default=True)
    p.add_argument("--no-reuse-session", dest="reuse_session",
                   action="store_false")
    p.add_argument("--extra-instructions",
                   help="optional addendum to the dispatched prompt")
    p.add_argument("--result-state",
                   help="state to land the section in while the sub-agent works "
                        "(default: schema's working state if declared)")
    p.add_argument("--force", action="store_true",
                   help="override the gate when section is terminal")
    c.add_pretty(p)
    p.set_defaults(func=cmd_dispatch)


def cmd_dispatch(args: argparse.Namespace) -> int:
    resolved = c.resolve_workspace_or_die(args, require_writable=True)
    if resolved is None:
        return 1
    _, ws_dir, filename = resolved

    if args.kill:
        return _cmd_dispatch_kill(args, ws_dir, filename)
    return _cmd_dispatch_spawn(args, ws_dir, filename)


def _cmd_dispatch_spawn(args, ws_dir, filename: str) -> int:
    agent = args.agent or "build"
    model = args.model or None

    with open_db(ws_dir / "db.sqlite3") as conn:
        schema = c.schema_or_die(conn)
        reconcile_all_dispatched(conn)

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

        # Validate explicit result-state.
        result_state = args.result_state
        if result_state is not None:
            try:
                validate_state(schema, result_state)
            except ValueError as e:
                c.emit_error({"error": str(e), "validStates": list(schema.names())})
                return 2
        else:
            result_state = schema.working_state()
            if result_state is None:
                # No working state declared. Default to the section's
                # current state and warn the agent.
                result_state = sec.state
                # Also: refuse to dispatch onto a terminal without
                # force (we'd be silently overwriting accepted content).
                if needs_force(sec.state, schema) and not args.force:
                    c.emit_error({
                        "error": "section is in a terminal state and no working state is "
                                 "declared in this doc's schema; pass force=true to dispatch "
                                 "a redo, or declare a working state.",
                        "currentState": sec.state,
                        "schemaStates": list(schema.names()),
                    })
                    return 1

        # Force gate: terminal -> needs force (regardless of working state).
        if needs_force(sec.state, schema) and not args.force:
            c.emit_error({
                **c.force_required_error(sec.state, schema),
                "hint": "Re-dispatching a terminal-state section overwrites it. "
                        "Confirm with force=true.",
            })
            return 1

        # If section is already in working state, refuse unless force.
        sd = schema.get(sec.state)
        if sd is not None and sd.working and not args.force:
            existing = conn.execute(
                """SELECT job_id, session_id, pid, started_at FROM assistants
                   WHERE section_id=? AND state IN ('queued', 'running')
                   ORDER BY started_at DESC LIMIT 1""",
                (uuid,),
            ).fetchone()
            err: dict[str, Any] = {
                "error": f"section {args.section_number!r} is already in working state {sec.state!r}",
                "currentState": sec.state,
                "hint": "Use markdown_dispatch { kill: true } to terminate the running job, "
                        "or pass force=true to spawn a duplicate.",
            }
            if existing:
                err["runningJob"] = {
                    "jobId": existing["job_id"],
                    "sessionId": existing["session_id"],
                    "pid": existing["pid"],
                    "startedAt": existing["started_at"],
                    "ageSec": now() - existing["started_at"],
                }
            c.emit_error(err)
            return 1

        addr = compute_section_number(conn, uuid)
        # Detect "is revision": section was in needs-attention state.
        is_revision = (
            schema.get(sec.state) is not None
            and (schema.get(sec.state).needsAttention)  # type: ignore[union-attr]
        )

        if args.session_id:
            reused_session = args.session_id
        elif args.reuse_session and sec.session_id:
            reused_session = sec.session_id
        else:
            reused_session = None

        prompt = build_prompt(
            filename=filename,
            section_number=addr.display,
            section_title=sec.title,
            seed=sec.seed,
            is_revision=is_revision,
            extra_instructions=args.extra_instructions,
        )

        job_id = gen_job_id()
        log_path = dispatch_log_path(ws_dir, job_id)
        conn.execute(
            """INSERT INTO assistants(job_id, section_id, agent, session_id,
                                      state, started_at, prompt, log_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, uuid, agent, reused_session, "queued", now(),
             prompt, str(log_path)),
        )
        conn.execute(
            """UPDATE sections SET state=?, updated_at=?, updated_by=?
               WHERE id=?""",
            (result_state, now(), "dispatch", uuid),
        )

    project_root = find_project_root()
    pid = spawn_opencode_dispatch(
        prompt=prompt, agent=agent, model=model,
        session_id=reused_session, log_path=log_path,
        cwd=cwd_path(project_root),
    )

    with open_db(ws_dir / "db.sqlite3") as conn:
        conn.execute(
            "UPDATE assistants SET state=?, pid=? WHERE job_id=?",
            ("running", pid, job_id),
        )

    response = {
        "jobId": job_id,
        "filename": filename,
        "sectionNumber": addr.display,
        "sectionState": result_state,
        "agent": agent,
        "model": model,
        "isRevision": is_revision,
        "reusedSession": reused_session,
        "pid": pid,
        "nextSteps": [
            f"Watch progress: markdown_outline {{ filename: {filename!r}, "
            f"source: 'workspace' }} -- shows section state.",
            f"When section state moves on, review with markdown_review.",
            f"To abort: markdown_dispatch {{ filename: {filename!r}, "
            f"sectionNumber: {addr.display!r}, kill: true }}.",
        ],
    }
    c.emit(response, args.pretty)
    return 0


def _cmd_dispatch_kill(args, ws_dir, filename: str) -> int:
    with open_db(ws_dir / "db.sqlite3") as conn:
        schema = c.schema_or_die(conn)
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

        killed = kill_running_dispatches_for_section(conn, uuid)

        new_state = sec.state
        # If the section was in a working state, reset to schema's initial.
        sd = schema.get(sec.state)
        if sd is not None and sd.working:
            initial = schema.initial_state()
            conn.execute(
                """UPDATE sections SET state=?, updated_at=?, updated_by=?
                   WHERE id=?""",
                (initial, now(), "dispatch-kill", uuid),
            )
            new_state = initial

    response = {
        "filename": filename,
        "sectionNumber": args.section_number,
        "killed": killed,
        "killedCount": len(killed),
        "sectionState": new_state,
    }
    if killed:
        response["nextSteps"] = [
            "Section reset to initial state. Re-dispatch with markdown_dispatch, "
            "or write manually with markdown_set."
        ]
    else:
        response["nextSteps"] = ["No running dispatch to kill."]
    c.emit(response, args.pretty)
    return 0
