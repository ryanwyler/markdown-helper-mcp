"""Shared helpers for cmd modules.

Functions here turn database rows into agent-visible JSON shapes, build
the standard changeReport + userSummary on every structural mutation,
do schema validation, and add the standard --pretty arg. Domain logic
goes in md/* modules, not here.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..ids import compute_section_number, resolve_section_number
from ..sections import (
    SectionRow,
    fetch_all_sections_in_order,
    fetch_section,
    section_depth,
)
from ..states import (
    PSEUDO_STATES,
    STATE_LOADED,
    STATE_READONLY,
    Schema,
    needs_force,
    validate_state,
)
from ..storage import (
    file_db_path,
    file_dir,
    file_workspace_exists,
    find_helper_root,
    get_schema,
    is_foreign_filename,
    meta_get,
    resolve_filename,
)
from ..util import now


# Meta key used to flag a workspace as opened from a foreign (cross-
# project) source. Mutating commands honor this and refuse to operate
# on the workspace, telling the agent to use markdown_save asFilename=
# to copy the doc into the current project first.
META_FOREIGN = "foreign"


def add_pretty(p: argparse.ArgumentParser) -> None:
    """Standard --pretty arg added to every subparser."""
    p.add_argument("--pretty", action="store_true",
                   help="pretty-print JSON output")


def emit(obj: Any, pretty: bool) -> None:
    """Print a JSON response on stdout."""
    print(json.dumps(obj, indent=2 if pretty else None))


def emit_error(obj: Any) -> None:
    """Print a JSON error on stderr."""
    print(json.dumps(obj), file=sys.stderr)


# ---------------------------------------------------------------------------
# Filename + workspace resolution (the standard preamble for nearly
# every cmd module)
# ---------------------------------------------------------------------------

def resolve_workspace_or_die(
    args: argparse.Namespace,
    *,
    require_writable: bool = False,
) -> tuple[Path, Path, str] | None:
    """Validate args.filename and return (helper_root, workspace_dir,
    canonical_filename), or print an error and return None.

    The agent's --filename gets resolved through `resolve_filename`,
    which accepts both cwd-relative paths AND absolute paths. The
    canonical form is what's re-assigned to args.filename so cmd
    modules and downstream responses always see the same string for
    the same file -- agent-facing form, not the on-disk encoding.

    The workspace_dir uses the INTERNAL workspace_key for path
    construction (which differs from canonical for foreign files:
    canonical is the absolute realpath, workspace_key is "ext/...").

    If `require_writable=True` and the file resolves outside the
    project, prints a clear error explaining the read-only constraint
    and how to escape it (markdown_save asFilename=...).
    """
    if not args.filename:
        emit_error({
            "error": "filename is required",
            "hint": "Pass a path relative to your cwd (e.g. 'docs/README.md') "
                    "or an absolute path for cross-project read-only access.",
        })
        return None
    try:
        resolved = resolve_filename(args.filename)
    except ValueError as e:
        emit_error({"error": str(e)})
        return None
    canonical = resolved.canonical
    args.filename = canonical  # agent-facing
    root = find_helper_root()
    # Workspace lookup uses the INTERNAL key (ext/... for foreign).
    if not file_workspace_exists(root, resolved.workspace_key):
        emit_error({
            "error": f"file {canonical!r} is not open in the workspace",
            "hint": "Use markdown_open (existing file) or markdown_create (new file).",
        })
        return None
    if require_writable and resolved.is_foreign:
        emit_error(_foreign_readonly_error(canonical))
        return None
    return root, file_dir(root, resolved.workspace_key), canonical


def _foreign_readonly_error(canonical: str) -> dict[str, Any]:
    """Standard error payload for any mutating call against a foreign
    (cross-project) workspace. Names the escape hatch explicitly."""
    return {
        "error": f"file {canonical!r} is outside the current project (read-only)",
        "readOnly": True,
        "hint": (
            "Cross-project files are mounted read-only. To edit, copy "
            "the doc into the current project by saving it under a "
            "cwd-relative name: "
            f"markdown_save {{ filename: {canonical!r}, asFilename: 'docs/<name>.md' }}. "
            "asFilename is relative to your cwd; the file is written there "
            "(creating directories if missing) and the workspace switches "
            "to the local copy, which you can then edit normally."
        ),
    }


def enforce_writable_or_die(canonical_filename: str) -> bool:
    """Block mutations against foreign workspaces. Call from every
    mutating cmd module (set, insert, delete, move, review, dispatch).

    Returns True if the workspace is writable. Emits an error and
    returns False if it's foreign.
    """
    if is_foreign_filename(canonical_filename):
        emit_error(_foreign_readonly_error(canonical_filename))
        return False
    return True


def resolve_section_or_die(
    conn: sqlite3.Connection,
    section_number: str,
) -> str | None:
    """Resolve an agent-supplied sectionNumber to a UUID, or print an
    error and return None.
    """
    if not section_number:
        emit_error({"error": "sectionNumber is required (e.g. 'A.1.2', 'B', '0')"})
        return None
    try:
        uuid = resolve_section_number(conn, section_number)
    except ValueError as e:
        emit_error({"error": str(e)})
        return None
    if uuid is None:
        emit_error({
            "error": f"sectionNumber {section_number!r} not found",
            "hint": "Use markdown_outline to see current section numbers.",
        })
        return None
    return uuid


def refuse_readonly_section_or_die(sec: SectionRow, section_number: str) -> bool:
    """Mutating ops must refuse readonly sections (foreign-doc
    sections) regardless of force. Returns True if writable, emits an
    error and returns False if readonly.
    """
    if sec.state == STATE_READONLY:
        emit_error({
            "error": f"section {section_number!r} is read-only "
                     f"(this doc was opened from another project)",
            "readOnly": True,
            "hint": "Use markdown_save with asFilename to copy this doc into "
                    "the current project, then edit the local copy.",
        })
        return False
    return True


# ---------------------------------------------------------------------------
# Section summarization (for outline / get / list responses)
# ---------------------------------------------------------------------------

def summarize_section(
    conn: sqlite3.Connection,
    sec: SectionRow,
    *,
    include_content: bool = False,
) -> dict[str, Any]:
    """Build the agent-visible JSON shape for a section.

    Includes sectionNumber (computed from chain position), title, state,
    word count, and seed. Optionally includes content (used by
    markdown_get).
    """
    addr = compute_section_number(conn, sec.id)
    depth = section_depth(conn, sec.id)
    out: dict[str, Any] = {
        "sectionNumber": addr.display,
        "depth": depth,
        "title": sec.title,
        "state": sec.state,
        "wordCount": sec.word_count,
    }
    if sec.seed is not None:
        out["seed"] = sec.seed
    if sec.session_id:
        out["sessionId"] = sec.session_id
    if sec.updated_at:
        out["updatedAt"] = sec.updated_at
    if sec.updated_by:
        out["updatedBy"] = sec.updated_by
    if include_content:
        out["content"] = sec.content
    return out


# ---------------------------------------------------------------------------
# Save-staleness signal (filename-level, lives on outline + list)
# ---------------------------------------------------------------------------

def save_staleness(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compute save-staleness fields for a single file."""
    last_str = meta_get(conn, "last_saved_at")
    last_ts = int(last_str) if last_str and last_str.isdigit() else 0
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM sections WHERE updated_at > ?", (last_ts,),
    ).fetchone()
    changed = int(row["c"]) if row else 0
    return {
        "needsSave": changed > 0,
        "sectionsChangedSinceSave": changed,
        "lastSavedAt": last_ts if last_ts > 0 else None,
    }


def save_nudge_step(staleness: dict[str, Any], filename: str) -> str | None:
    """Format a nextSteps line nudging the agent to save."""
    if not staleness.get("needsSave"):
        return None
    n = staleness["sectionsChangedSinceSave"]
    plural = "section" if n == 1 else "sections"
    return (
        f"{n} {plural} updated since the last save. "
        f"Call markdown_save {{ filename: {filename!r} }} to write to the file."
    )


# ---------------------------------------------------------------------------
# Schema convenience: every cmd module that takes/validates a state
# param goes through these.
# ---------------------------------------------------------------------------

def schema_or_die(conn: sqlite3.Connection) -> Schema:
    """Load the workspace's declared schema, falling back to default."""
    return get_schema(conn)


def validate_state_or_die(schema: Schema, name: str | None) -> bool:
    """If `name` is provided, validate it against the schema. Returns
    True if valid (or None passed); emits an error and returns False
    otherwise."""
    if name is None:
        return True
    try:
        validate_state(schema, name)
        return True
    except ValueError as e:
        emit_error({
            "error": str(e),
            "validStates": list(schema.names()),
        })
        return False


def force_required_error(state: str, schema: Schema) -> dict[str, Any]:
    """Standard error payload for force-required overwrite refusal."""
    return {
        "error": f"section is in terminal state {state!r}; pass force=true to overwrite",
        "currentState": state,
        "hint": "Overwriting a terminal-state section is destructive. "
                "Confirm with force=true.",
    }


# ---------------------------------------------------------------------------
# Outline snapshots and changeReport
# ---------------------------------------------------------------------------
#
# A snapshot is a list of (uuid, sectionNumber, title, wordCount) for
# every section in render order, taken before and after a structural
# mutation. The diff produces the standard {inserted, deleted, shifted,
# modified} changeReport plus a userSummary string.

@dataclass(frozen=True)
class OutlineEntry:
    uuid: str
    sectionNumber: str
    title: str | None
    depth: int
    wordCount: int
    contentHash: str  # short hash of body for change detection
    updatedAt: int


def take_outline_snapshot(conn: sqlite3.Connection) -> list[OutlineEntry]:
    """Capture the current outline as a flat ordered list keyed by
    UUID. Used as the BEFORE state for diffing structural mutations."""
    import hashlib as _hashlib
    out: list[OutlineEntry] = []
    for sec in fetch_all_sections_in_order(conn):
        addr = compute_section_number(conn, sec.id)
        depth = section_depth(conn, sec.id)
        body = sec.content or ""
        ch = _hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
        out.append(OutlineEntry(
            uuid=sec.id,
            sectionNumber=addr.display,
            title=sec.title,
            depth=depth,
            wordCount=sec.word_count,
            contentHash=ch,
            updatedAt=sec.updated_at or 0,
        ))
    return out


def outline_summary(snapshot: list[OutlineEntry]) -> list[dict[str, Any]]:
    """Compact post-mutation outline for inclusion in mutation responses.

    Each entry carries sectionNumber, depth, title, wordCount -- enough
    for an agent to re-plan against shifted addresses without a separate
    markdown_outline call. Note: NO state/seed -- this is a structural
    snapshot, not a workspace view.
    """
    return [
        {
            "sectionNumber": e.sectionNumber,
            "depth": e.depth,
            "title": e.title,
            "wordCount": e.wordCount,
        }
        for e in snapshot
    ]


# Threshold above which `shifted` is omitted in favor of just the
# outline. Keeps the response from drowning in shift mappings.
MAX_SHIFTED_IN_REPORT = 5


def compute_change_report(
    before: list[OutlineEntry],
    after: list[OutlineEntry],
    filename: str,
) -> dict[str, Any]:
    """Diff two outline snapshots into the standard changeReport shape.

    Returns {changeReport, userSummary}.

    changeReport keys:
      inserted: [{sectionNumber, title}] for sections in `after` but
        not `before` (new uuid).
      deleted:  [{sectionNumber, title}] for sections in `before` but
        not `after` (uuid removed).
      shifted:  [{from, to, title}] for sections where uuid is in both
        but sectionNumber changed. Omitted entirely if >5.
      modified: [{sectionNumber, wordDelta}] for sections where uuid
        is in both, sectionNumber unchanged, but wordCount differs.

    userSummary: pre-rendered string for human readers.
        <filename>:
          + <sn> "<title>"
          - <sn> "<title>"
          ~ <from> -> <to> "<title>"
          * <sn>  (+/-N words)
    """
    before_by_uuid = {e.uuid: e for e in before}
    after_by_uuid = {e.uuid: e for e in after}

    inserted: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    shifted: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []

    for entry in after:
        prev = before_by_uuid.get(entry.uuid)
        if prev is None:
            inserted.append({
                "sectionNumber": entry.sectionNumber,
                "title": entry.title,
            })
        else:
            if prev.sectionNumber != entry.sectionNumber:
                shifted.append({
                    "from": prev.sectionNumber,
                    "to": entry.sectionNumber,
                    "title": entry.title,
                })
            # Content change detection: prefer hash, fall back to word
            # count delta. Hash catches edits where word count is
            # unchanged (e.g. small patches).
            if (prev.contentHash != entry.contentHash
                    and prev.sectionNumber == entry.sectionNumber):
                modified.append({
                    "sectionNumber": entry.sectionNumber,
                    "wordDelta": entry.wordCount - prev.wordCount,
                })

    for entry in before:
        if entry.uuid not in after_by_uuid:
            deleted.append({
                "sectionNumber": entry.sectionNumber,
                "title": entry.title,
            })

    change_report: dict[str, Any] = {
        "inserted": inserted,
        "deleted": deleted,
        "modified": modified,
    }
    # Omit the shifted array if too noisy -- agent re-plans against the
    # outline. Always include the COUNT so the agent knows shifts
    # happened even when we didn't list them.
    if len(shifted) <= MAX_SHIFTED_IN_REPORT:
        change_report["shifted"] = shifted
    else:
        change_report["shiftedCount"] = len(shifted)

    user_summary = _render_user_summary(
        filename, inserted, deleted, shifted, modified,
    )
    return {
        "changeReport": change_report,
        "userSummary": user_summary,
        "outline": outline_summary(after),
    }


def _render_user_summary(
    filename: str,
    inserted: list[dict[str, Any]],
    deleted: list[dict[str, Any]],
    shifted: list[dict[str, Any]],
    modified: list[dict[str, Any]],
) -> str:
    """Render the changeReport as a multi-line human-readable string.

    Format:
      <filename>:
        + <sn> "<title>"
        - <sn> "<title>"
        ~ <from> -> <to> "<title>"
        * <sn>  (+N words)

    If there are no changes, returns "<filename>:\n  (no changes)".
    """
    lines: list[str] = [f"{filename}:"]
    any_change = False

    for item in inserted:
        any_change = True
        title = item.get("title") or "(headless)"
        lines.append(f'  + {item["sectionNumber"]} "{title}"')

    for item in deleted:
        any_change = True
        title = item.get("title") or "(headless)"
        lines.append(f'  - {item["sectionNumber"]} "{title}"')

    if len(shifted) <= MAX_SHIFTED_IN_REPORT:
        for item in shifted:
            any_change = True
            title = item.get("title") or "(headless)"
            lines.append(f'  ~ {item["from"]} -> {item["to"]} "{title}"')
    else:
        any_change = True
        lines.append(f'  ~ {len(shifted)} sections shifted (re-plan against outline)')

    for item in modified:
        any_change = True
        delta = item["wordDelta"]
        sign = "+" if delta >= 0 else ""
        lines.append(f'  * {item["sectionNumber"]}  ({sign}{delta} words)')

    if not any_change:
        lines.append("  (no changes)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Counts (re-exported for convenience by callers that build responses)
# ---------------------------------------------------------------------------

def collect_state_counts_from_workspace(conn: sqlite3.Connection) -> dict[str, int]:
    """Tally state counts across the whole workspace."""
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT state, COUNT(*) AS c FROM sections GROUP BY state"
    ).fetchall():
        counts[row["state"]] = int(row["c"])
    return dict(sorted(counts.items()))


# Re-export for convenience.
__all__ = [
    "add_pretty",
    "emit",
    "emit_error",
    "resolve_workspace_or_die",
    "resolve_section_or_die",
    "refuse_readonly_section_or_die",
    "enforce_writable_or_die",
    "summarize_section",
    "save_staleness",
    "save_nudge_step",
    "schema_or_die",
    "validate_state_or_die",
    "force_required_error",
    "OutlineEntry",
    "take_outline_snapshot",
    "compute_change_report",
    "collect_state_counts_from_workspace",
    "now",
    "file_db_path",
    "find_helper_root",
    "META_FOREIGN",
    "PSEUDO_STATES",
    "STATE_LOADED",
    "STATE_READONLY",
]
