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
from ..states import PSEUDO_STATES, STATE_LOADED, STATE_READONLY, is_terminal
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
                        "(workspace source only). Default filter excludes "
                        "the 'loaded' pseudo-state and terminal states, so "
                        "`markdown_outline` defaults to showing live work "
                        "only. Pass --filter-states='all' to disable the "
                        "default and see everything. Names support negation: "
                        "`!loaded,!done` is equivalent to the default. "
                        "Mixing positive and negative names is allowed: "
                        "`in-progress,!done` shows in-progress only.")
    p.add_argument("--title-contains",
                   help="substring filter on section titles (case-insensitive). "
                        "Applied after --filter-states. Workspace and disk.")
    p.add_argument("--tags-contain",
                   help="comma-separated list of tags; matches sections "
                        "that carry ALL of them. Tags are lowercase-"
                        "normalized; e.g., 'billing,v1' matches sections "
                        "with both. Workspace only -- disk mode has no "
                        "access to tags.")
    p.add_argument("--format", choices=["structured", "compact"],
                   default="structured",
                   help="structured (default): list of section objects. "
                        "compact: list of single-line strings "
                        "(`A | depth=1 | done | 65w | Title`) for "
                        "token-efficient outlines of large docs.")
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
    flat = _apply_state_filter(flat, args.filter_states, schema)
    flat = _apply_title_contains(flat, args.title_contains)
    flat = _apply_tags_contain(flat, args.tags_contain)

    section_dicts = _serialize_sections(flat, args.format, include_state=True)

    counts: dict[str, int] = {}
    for s in flat:
        if s.state:
            counts[s.state] = counts.get(s.state, 0) + 1

    response: dict = {
        "filename": filename,
        "source": "workspace",
        "schema": schema.to_response(),
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
    # Disk has no schema (no states beyond `loaded`); only title-filter
    # applies. filterStates is silently ignored in disk mode.
    flat = _apply_title_contains(flat, args.title_contains)
    section_dicts = _serialize_sections(flat, args.format, include_state=False)

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
# Filter helpers
# ---------------------------------------------------------------------------

_DEFAULT_EXCLUDED_PSEUDO = {STATE_LOADED}


def _parse_filter_spec(spec: str | None, schema) -> tuple[set[str] | None, set[str]]:
    """Parse the filterStates DSL into (include, exclude) sets.

    Grammar:
      None / empty       -> DEFAULT: exclude `loaded` + all terminal states.
      "all"              -> include everything (no filtering).
      "name1,name2"      -> include ONLY these (positive list overrides default).
      "!name,!name"      -> all-except-named (negative list = default + extras).
      mixed pos+neg      -> positive list, with extra excludes layered on.

    Example: an agent says filterStates="in-progress,!done" (silly but
    valid) -> only in-progress matches anyway because positive list is
    authoritative. The negative list only kicks in when positives are
    empty.

    Returns (include_set_or_None_meaning_unconstrained, exclude_set).
    """
    if spec is None or spec.strip() == "":
        # Default: exclude loaded + every terminal state.
        terminal_states = {s for s in schema.names() if is_terminal(s, schema)}
        return None, _DEFAULT_EXCLUDED_PSEUDO | terminal_states
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if parts == ["all"]:
        return None, set()
    include: set[str] = set()
    exclude: set[str] = set()
    for p in parts:
        if p.startswith("!"):
            name = p[1:].strip()
            if name:
                exclude.add(name)
        else:
            include.add(p)
    return (include if include else None), exclude


def _apply_state_filter(flat, filter_spec, schema):
    """Filter sections by state per the filterStates DSL."""
    include, exclude = _parse_filter_spec(filter_spec, schema)
    out = []
    for s in flat:
        if include is not None and s.state not in include:
            continue
        if s.state in exclude:
            continue
        out.append(s)
    return out


def _apply_title_contains(flat, needle: str | None):
    """Substring filter on title (case-insensitive). Headless preamble
    sections (title is None) are excluded when a filter is set."""
    if not needle:
        return flat
    needle_lower = needle.lower()
    return [s for s in flat if s.title and needle_lower in s.title.lower()]


def _apply_tags_contain(flat, tags_spec: str | None):
    """Filter sections to those carrying ALL of the named tags. Tags
    are lowercase-normalized when stored, so we lowercase the query.
    Disk mode has no tags -- everything will fail to match, so the
    filter effectively yields no results.
    """
    if not tags_spec:
        return flat
    required = {t.strip().lower() for t in tags_spec.split(",") if t.strip()}
    if not required:
        return flat
    out = []
    for s in flat:
        section_tags = set(getattr(s, "tags", None) or [])
        if required.issubset(section_tags):
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Section -> dict / compact string
# ---------------------------------------------------------------------------

def _serialize_sections(flat, format_: str, *, include_state: bool):
    """Render the section list per `format_`.

    structured (default): list of dicts (one per section).
    compact: list of single-line strings, one per section, of the form
      `<sectionNumber> | depth=<n> | <state> | <words>w | <title>`.
      Headless preamble shows `(headless)` for the title. State omitted
      from compact when include_state=False (disk mode).
    """
    if format_ == "compact":
        return [_compact_line(s, include_state=include_state) for s in flat]
    return [_serialize_section(s, include_state=include_state) for s in flat]


def _compact_line(s, *, include_state: bool) -> str:
    title = s.title if s.title else "(headless)"
    words = count_words(s.body)
    tags = getattr(s, "tags", None) or []
    tags_segment = f" | [{','.join(tags)}]" if tags else ""
    if include_state and s.state:
        return (f"{s.section_number} | depth={s.depth} | {s.state} | "
                f"{words}w | {title}{tags_segment}")
    return f"{s.section_number} | depth={s.depth} | {words}w | {title}{tags_segment}"


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
    tags = getattr(s, "tags", None) or []
    if tags:
        out["tags"] = tags
    return out
