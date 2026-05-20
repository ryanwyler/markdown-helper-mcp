"""markdown_tag: edit a section's tags without touching its body.

Tags are an orthogonal axis to state and content (state = pipeline
position; tags = topic). This verb mirrors `markdown_review` for
state-only transitions: tags-only edits with no body rewrite, no
force gate. Use it when you want to label content that's already
written.

Three mutually-exclusive operations per write:
  --set    [a,b]   : REPLACE the tag set entirely
  --add    [a,b]   : UNION with existing tags
  --remove [a,b]   : SET DIFFERENCE; existing minus these

Exactly one of set/add/remove per write. Mixing them in one entry
is a validation error.

Single-section form:
  markdown_tag --filename X --section-number A --add billing,urgent

Batch form (one transaction, all-or-nothing):
  markdown_tag --filename X --writes-json '[
    {"sectionNumber":"A","add":["spec"]},
    {"sectionNumber":"B","set":["spec","draft"]},
    {"sectionNumber":"C","remove":["wip"]}
  ]'

Returns a per-section summary {sectionNumber, before, after, op}
plus the standard changeReport (modified entries only -- tag edits
don't shift section numbers, so `shifted` is always empty).
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from . import _common as c
from ..revisions import record_revision
from ..sections import fetch_section, update_section_content
from ..states import STATE_READONLY
from ..storage import open_db
from ..util import normalize_tags, now


# Sentinel for "operation not provided" -- distinguishes None from
# legitimate empty-list. Per-entry validation rejects "no op specified."
_OP_UNSET = object()


def register_parser(sub) -> None:
    p = sub.add_parser("tag",
                       help="edit a section's tags (set/add/remove); no body rewrite")
    p.add_argument("--filename", required=True)
    p.add_argument("--section-number",
                   help="sectionNumber OR title. Required unless --writes-json.")
    p.add_argument("--parent-section",
                   help="scope title-resolution to the named section's "
                        "subtree (use when --section-number is a title "
                        "that matches multiple sections).")

    # Exactly one of set/add/remove for the single-section form.
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--set", dest="op_set",
                     help="REPLACE the tag set. JSON array OR comma-separated.")
    grp.add_argument("--add", dest="op_add",
                     help="UNION with existing tags. JSON array OR comma-separated.")
    grp.add_argument("--remove", dest="op_remove",
                     help="REMOVE from existing tags. JSON array OR comma-separated.")

    p.add_argument("--writes-json",
                   help="JSON array of {sectionNumber, set|add|remove, parentSection?} "
                        "entries. Mutually exclusive with --section-number form.")
    p.add_argument("--by", help="author attribution; recorded in revision history")
    p.add_argument("--session-id", dest="session_id")
    c.add_pretty(p)
    p.set_defaults(func=cmd_tag)


def _parse_tag_arg(raw: str) -> list[str]:
    """Accept either JSON array form (`'["a","b"]'`) or comma-separated
    form (`a,b`) and return a list of strings (pre-normalization).
    Empty string -> empty list (caller decides whether that's valid).
    """
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"tag list must be valid JSON or comma-separated: {e}")
        if not isinstance(parsed, list):
            raise ValueError(f"tag list must be a JSON array; got {type(parsed).__name__}")
        return parsed
    # comma-separated
    return [t.strip() for t in raw.split(",") if t.strip()]


class _TagError(Exception):
    def __init__(self, payload: dict[str, Any], code: int = 1) -> None:
        self.payload = payload
        self.code = code


def cmd_tag(args: argparse.Namespace) -> int:
    resolved = c.resolve_workspace_or_die(args, require_writable=True)
    if resolved is None:
        return 1
    _, ws_dir, filename = resolved

    # Build the entries list. Single-section form -> list of one.
    entries: list[dict[str, Any]] = []
    if args.writes_json:
        if args.section_number or args.op_set or args.op_add or args.op_remove:
            c.emit_error({
                "error": "--writes-json is mutually exclusive with the "
                         "single-section flags (--section-number / "
                         "--set / --add / --remove)",
            })
            return 2
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
            c.emit_error({"error": "sectionNumber is required (or use --writes-json)"})
            return 2
        ops = [
            ("set", args.op_set),
            ("add", args.op_add),
            ("remove", args.op_remove),
        ]
        chosen = [(k, v) for k, v in ops if v is not None]
        if len(chosen) != 1:
            c.emit_error({
                "error": "exactly one of --set / --add / --remove is required",
            })
            return 2
        op_name, op_raw = chosen[0]
        try:
            tags_list = _parse_tag_arg(op_raw)
        except ValueError as e:
            c.emit_error({"error": str(e)})
            return 2
        entry: dict[str, Any] = {
            "sectionNumber": args.section_number,
            op_name: tags_list,
        }
        if args.parent_section:
            entry["parentSection"] = args.parent_section
        entries = [entry]

    if not entries:
        c.emit_error({"error": "no tag entries provided"})
        return 2

    by = args.by or "primary"
    session_id = args.session_id

    with open_db(ws_dir / "db.sqlite3") as conn:
        before_snapshot = c.take_outline_snapshot(conn)

        try:
            per_section = _apply_tag_writes(
                conn, entries, by=by, session_id=session_id,
            )
        except _TagError as e:
            c.emit_error(e.payload)
            return e.code

        after_snapshot = c.take_outline_snapshot(conn)
        report = c.compute_change_report(
            before_snapshot, after_snapshot, filename,
            conn=conn, by=by,
        )

    # Tag mutations don't touch content/sectionNumber/wordCount, so the
    # outline-snapshot diff sees no change and renders "(no changes)".
    # Override with a tag-specific summary derived from per_section.
    user_summary = _render_tag_summary(filename, per_section)

    response: dict[str, Any] = {
        "filename": filename,
        "writeCount": len(entries),
        "sections": per_section,
        "changeReport": report["changeReport"],
        "userSummary": user_summary,
    }
    c.emit(response, args.pretty)
    return 0


def _render_tag_summary(filename: str, per_section: list[dict[str, Any]]) -> str:
    """Render a tag-mutation userSummary.

    Format:
      <filename>:
        # A "Alpha"  add: [security] -> [billing, security, spec, urgent]
        # B "Beta"   set: [draft, spec]
        # C "Gamma"  no-op
    """
    lines = [f"{filename}:"]
    any_change = False
    for sec in per_section:
        sn = sec["sectionNumber"]
        title = sec.get("title") or "(headless)"
        op = sec["op"]
        op_arg = sec.get("opArg") or []
        op_arg_str = ",".join(op_arg) if op_arg else "(empty)"
        if sec.get("noOp"):
            lines.append(f'  # {sn} "{title}"  {op}: {op_arg_str} (no-op, tags unchanged)')
        else:
            any_change = True
            after = sec.get("after") or []
            after_str = ",".join(after) if after else "(none)"
            lines.append(f'  # {sn} "{title}"  {op}: {op_arg_str} -> [{after_str}]')
    if not any_change and len(lines) == 1:
        lines.append("  (no changes)")
    return "\n".join(lines)


def _apply_tag_writes(
    conn,
    entries: list[dict[str, Any]],
    *,
    by: str,
    session_id: str | None,
) -> list[dict[str, Any]]:
    """Validate every entry up-front, then apply atomically.

    Returns a per-section summary list:
      [{sectionNumber, title, before: [...], after: [...], op: "set|add|remove"}]

    Raises _TagError on any validation failure -- no writes happen if
    any entry is bad (same all-or-nothing semantics as markdown_set's
    batch path).
    """
    from ..ids import compute_section_number

    # PASS 1: validate every entry, resolve uuids, compute target tag sets.
    plans: list[dict[str, Any]] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise _TagError({"error": f"writes[{i}]: must be an object"}, code=2)

        sn = entry.get("sectionNumber")
        if not sn:
            raise _TagError(
                {"error": f"writes[{i}]: sectionNumber is required"}, code=2,
            )

        # Exactly one of set/add/remove per entry.
        ops_present = [k for k in ("set", "add", "remove") if k in entry]
        if len(ops_present) != 1:
            raise _TagError({
                "error": f"writes[{i}]: exactly one of set/add/remove is required "
                         f"(got {ops_present or 'none'})",
            }, code=2)
        op_name = ops_present[0]
        op_value = entry[op_name]
        if not isinstance(op_value, list):
            raise _TagError({
                "error": f"writes[{i}]: {op_name} must be a list of strings",
            }, code=2)

        # Resolve section.
        parent_scope = entry.get("parentSection")
        from ..ids import resolve_section_number
        uuid = None
        try:
            uuid = resolve_section_number(conn, sn)
        except ValueError:
            pass
        if uuid is None:
            if parent_scope:
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
                        raise _TagError({
                            "error": f"writes[{i}]: parentSection "
                                     f"{parent_scope!r} matches "
                                     f"{len(scope_rows)} sections; ambiguous",
                            "hint": "Use a sectionNumber for parentSection, "
                                    "or pick an unambiguous ancestor title.",
                        }, code=2)
                if scope_uuid is None:
                    raise _TagError({
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
                cands = [compute_section_number(conn, u).display for u in match_uuids]
                raise _TagError({
                    "error": f"writes[{i}]: title {sn!r} matches "
                             f"{len(match_uuids)} sections; ambiguous",
                    "candidates": cands,
                    "hint": "Pass an unambiguous title, a sectionNumber, "
                            "or add `parentSection: <ancestor>` to the "
                            "entry to narrow the search.",
                }, code=2)
        if uuid is None:
            raise _TagError({
                "error": f"writes[{i}]: {sn!r} is neither a sectionNumber "
                         f"nor an exact title match",
                "hint": "Use markdown_outline to see current titles and numbers.",
            }, code=1)

        sec = fetch_section(conn, uuid)
        if sec is None:
            raise _TagError(
                {"error": f"writes[{i}]: section row missing for {sn!r}"},
                code=1,
            )
        if sec.state == STATE_READONLY:
            raise _TagError({
                "error": f"writes[{i}]: section {sn!r} is read-only",
                "readOnly": True,
                "hint": "Use markdown_save asFilename to copy this doc into "
                        "the current project, then edit the local copy.",
            }, code=1)

        # Normalize the op's tag list (validates length, charset, etc).
        try:
            op_tags_norm = normalize_tags(op_value)
        except ValueError as e:
            raise _TagError({"error": f"writes[{i}]: {e}"}, code=2)

        # Compute the new tag set based on the operation.
        existing = set(sec.tags or [])
        if op_name == "set":
            new_tags_set = set(op_tags_norm)
        elif op_name == "add":
            new_tags_set = existing | set(op_tags_norm)
        else:  # remove
            new_tags_set = existing - set(op_tags_norm)
        new_tags = sorted(new_tags_set)

        plans.append({
            "uuid": uuid,
            "sec": sec,
            "op": op_name,
            "op_arg": op_tags_norm,
            "new_tags": new_tags,
        })

    # PASS 2: apply.
    summary: list[dict[str, Any]] = []
    timestamp = now()
    for plan in plans:
        sec = plan["sec"]
        uuid = plan["uuid"]
        before_tags = list(sec.tags or [])
        new_tags = plan["new_tags"]

        # No-op detection: tag set unchanged AND no audit event needed.
        # Skip the write entirely to keep revision history meaningful.
        is_noop = before_tags == new_tags

        if not is_noop:
            update_section_content(
                conn, uuid,
                content=sec.content,
                tags=new_tags,
                updated_by=by,
                session_id=session_id,
                now=timestamp,
            )
            # Record an audit revision so the change shows up in history.
            # Note in the revision is a compact descriptor of what
            # happened ("add: billing,urgent" or "remove: wip").
            op_arg_str = ",".join(plan["op_arg"]) if plan["op_arg"] else "(empty)"
            record_revision(
                conn, section_id=uuid, content=sec.content, state=sec.state,
                notes=f"tag {plan['op']}: {op_arg_str}",
                by=by,
            )

        addr = compute_section_number(conn, uuid)
        summary.append({
            "sectionNumber": addr.display,
            "title": sec.title or "(headless)",
            "before": before_tags,
            "after": new_tags,
            "op": plan["op"],
            "opArg": plan["op_arg"],
            "noOp": is_noop,
        })

    return summary
