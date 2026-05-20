"""Shared helpers for cmd modules.

Functions here turn database rows into agent-visible JSON shapes, build
the standard changeReport + userSummary on every structural mutation,
do schema validation, and add the standard --pretty arg. Domain logic
goes in md/* modules, not here.
"""

from __future__ import annotations

import argparse
import json
import re
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
    update_section_content,
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

    Strict: only accepts section-number form ("A", "A.1.2", "0"). For
    insert/move position targets that should also accept titles, use
    resolve_section_handle.
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


def resolve_section_handle(
    conn: sqlite3.Connection,
    handle: str,
    *,
    parent_scope: str | None = None,
    arg_name: str = "target",
) -> str | None:
    """Resolve a position-target HANDLE to a UUID.

    The handle is whatever the agent typed for `before` / `after` /
    `under`. We try in this order:

      1. Section-number form ("A", "A.1.2", "0") -- exact address.
      2. Exact title match -- the natural form when sections are
         being referred to by name (which is more stable than numbers
         after structural mutations).

    Title-resolution semantics:
      - Match is case-sensitive and exact (no substring).
      - If multiple sections share the title, errors with a list of
        candidates (sectionNumber + parent title for context) and a
        hint to narrow via `parentSection: <sectionNumber>`.
      - `parent_scope` (a section-number) restricts the search to the
        named section's subtree (its descendants + itself, but a
        target IS the parent itself is rejected -- that would never
        be useful for an insert/move position).

    Returns the UUID, or None after emitting an error.
    """
    if not handle:
        emit_error({"error": f"{arg_name} is required"})
        return None

    # Pass 1: try section-number form. The address grammar is strict
    # (letters + dots), so a typo like "Active execution tracker"
    # raises ValueError; we swallow that and fall through to title.
    try:
        uuid_by_number = resolve_section_number(conn, handle)
        if uuid_by_number is not None:
            return uuid_by_number
    except ValueError:
        pass

    # Pass 2: title-resolution.
    if parent_scope:
        # Resolve parent_scope the same way -- accept sectionNumber OR
        # exact title. R1 says titles are stable, so agents will type
        # `parentSection: "Alpha"` not `parentSection: "A.1"`. Number
        # first, then title. Ambiguous parent title is a real error
        # (pick an unambiguous ancestor).
        scope_uuid: str | None = None
        try:
            scope_uuid = resolve_section_number(conn, parent_scope)
        except ValueError:
            pass
        if scope_uuid is None:
            scope_rows = conn.execute(
                "SELECT id FROM sections WHERE title = ?", (parent_scope,),
            ).fetchall()
            if len(scope_rows) == 1:
                scope_uuid = scope_rows[0]["id"]
            elif len(scope_rows) > 1:
                emit_error({
                    "error": f"parentSection {parent_scope!r} matches "
                             f"{len(scope_rows)} sections; ambiguous",
                    "hint": "Use a sectionNumber for parentSection, "
                            "or pick an unambiguous ancestor title.",
                })
                return None
        if scope_uuid is None:
            emit_error({
                "error": f"parentSection {parent_scope!r} not found "
                         f"(tried sectionNumber AND title)",
                "hint": "Use markdown_outline to see current section "
                        "titles and numbers.",
            })
            return None
        # Walk descendants of scope_uuid and collect title matches.
        candidates = _find_title_in_subtree(conn, scope_uuid, handle)
    else:
        # Global title search.
        rows = conn.execute(
            "SELECT id FROM sections WHERE title = ?",
            (handle,),
        ).fetchall()
        candidates = [r["id"] for r in rows]

    if len(candidates) == 1:
        return candidates[0]

    if not candidates:
        scope_phrase = f" in subtree of {parent_scope!r}" if parent_scope else ""
        emit_error({
            "error": f"no section{scope_phrase} with title {handle!r} (and not a valid sectionNumber either)",
            "hint": "Titles are matched exactly (case-sensitive). "
                    "Use markdown_outline to see current titles, or pass a "
                    "sectionNumber like 'A' / 'A.1.2'.",
        })
        return None

    # Ambiguity: build a candidates list with each section's address +
    # its parent's title (so the agent can pick by context).
    cand_details = []
    for uuid in candidates:
        addr = compute_section_number(conn, uuid)
        row = conn.execute(
            "SELECT s.title AS title, p.id AS parent_id, p.title AS parent_title "
            "FROM sections s LEFT JOIN sections p ON s.parent_id = p.id "
            "WHERE s.id = ?",
            (uuid,),
        ).fetchone()
        parent_sn: str | None = None
        if row and row["parent_id"]:
            parent_sn = compute_section_number(conn, row["parent_id"]).display
        cand_details.append({
            "sectionNumber": addr.display,
            "title": row["title"] if row else handle,
            "parentSectionNumber": parent_sn,
            "parentTitle": row["parent_title"] if row else None,
        })

    emit_error({
        "error": f"title {handle!r} matches {len(candidates)} sections; ambiguous",
        "candidates": cand_details,
        "hint": "Narrow with parentSection: <sectionNumber> (use one of "
                f"the parentSectionNumber values above), or pass an "
                f"exact sectionNumber like 'A.1.2'.",
    })
    return None


def _find_title_in_subtree(
    conn: sqlite3.Connection,
    scope_uuid: str,
    title: str,
) -> list[str]:
    """Return UUIDs of sections under `scope_uuid` (descendants only,
    not the scope itself) whose title matches `title` exactly."""
    # BFS by parent_id.
    out: list[str] = []
    frontier = [scope_uuid]
    visited: set[str] = set()
    while frontier:
        nxt: list[str] = []
        for pid in frontier:
            if pid in visited:
                continue
            visited.add(pid)
            rows = conn.execute(
                "SELECT id, title FROM sections WHERE parent_id = ?",
                (pid,),
            ).fetchall()
            for r in rows:
                if r["title"] == title:
                    out.append(r["id"])
                nxt.append(r["id"])
        frontier = nxt
    return out


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
    word count, seed, and tags. Optionally includes content (used by
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
    if sec.tags:
        out["tags"] = sec.tags
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
    plural = "section has" if n == 1 else "sections have"
    last_ts = staleness.get("lastSavedAt")
    if last_ts:
        age_s = max(0, now() - int(last_ts))
        if age_s < 60:
            age_phrase = f"last save was {age_s}s ago"
        elif age_s < 3600:
            age_phrase = f"last save was {age_s // 60}m ago"
        else:
            age_phrase = f"last save was {age_s // 3600}h{(age_s % 3600) // 60}m ago"
    else:
        age_phrase = "never saved yet"
    return (
        f"{n} {plural} changed since last save ({age_phrase}). "
        f"Call markdown_save {{ filename: {filename!r} }} to flush to disk."
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


# Threshold above which `shifted` is omitted in favor of just the
# outline. Keeps the response from drowning in shift mappings.
MAX_SHIFTED_IN_REPORT = 5


# ---------------------------------------------------------------------------
# §-reference auto-rewrite
# ---------------------------------------------------------------------------
#
# When a structural mutation shifts section numbers (B -> C, B.1 -> C.1,
# etc), any cross-reference in the body of any OTHER section that wrote
# the old number as `§B.1` goes stale. Agents hate this -- they spend
# turns chasing down stale references after every insert/move/delete.
#
# Solution: after a mutation, scan every section's body for `§<old>`
# tokens that match a shift in the changeReport, and rewrite them to
# `§<new>`. Two-pass with sentinels to handle chained collisions
# (e.g. C.1 -> D.1 must not get re-rewritten by the D.1 -> E.1 pass).
#
# Reference syntax recognised:
#   §A          top-level letter
#   §B.1        depth-2
#   §B.1.2.3    arbitrarily deep
#   §0          the headless preamble
# A trailing word-boundary keeps `§B.1` from gobbling `§B.10` and
# vice versa.
#
# Skipped contexts:
#   - fenced code blocks (``` and ~~~): treated as quoted material;
#     leave the literal text intact. Agents quote source / examples
#     inside fences and rewriting them would corrupt the quote.
#   - HTML comments (<!-- ... -->): the trailer block lives in an
#     HTML comment at end-of-file, but per-section bodies generally
#     don't carry them; still, treat as opaque to be safe.
#
# Inline code spans (`§B.1`) ARE rewritten -- backticks-in-prose
# represent a typographically emphasised live reference, not quoted
# material.
#
# The regex below intentionally matches `§<id>` ONLY, not bare `B.1`
# tokens. The guide instructs agents to use the §-sigil for any
# section reference in persistent content; bare tokens are agent's
# responsibility (they might be version numbers, file paths, etc).

# Matches §<id> where <id> is either "0" or one uppercase letter
# optionally followed by .N segments. Letter case is restricted to
# A-Z because top-level letters come from _letter_for_index().
_REF_PATTERN = re.compile(
    r"§(?P<id>0|[A-Z](?:\.\d+)*)(?=\W|$)",
)

# Matches a fenced code block (``` or ~~~ on its own line, span until
# the matching closing fence). DOTALL because fences span newlines.
# Non-greedy so adjacent fences don't get merged. Anchored to line
# start for the opening fence to avoid matching ``` inside prose.
_FENCE_PATTERN = re.compile(
    r"(?ms)^(?P<fence>```|~~~)[^\n]*\n.*?^(?P=fence)\s*$",
)


def _mask_fences(text: str) -> tuple[str, list[str]]:
    """Replace fenced code blocks with sentinel tokens.

    Returns (masked_text, originals). Apply the rewrite to
    masked_text, then unmask via _unmask_fences(result, originals).
    """
    originals: list[str] = []

    def _repl(m: re.Match[str]) -> str:
        idx = len(originals)
        originals.append(m.group(0))
        return f"\x00MDH_FENCE_{idx}\x00"

    masked = _FENCE_PATTERN.sub(_repl, text)
    return masked, originals


def _unmask_fences(text: str, originals: list[str]) -> str:
    """Restore fenced code blocks from sentinel tokens."""
    for idx, original in enumerate(originals):
        text = text.replace(f"\x00MDH_FENCE_{idx}\x00", original)
    return text


def rewrite_section_references(
    text: str,
    shift_map: dict[str, str],
) -> tuple[str, int]:
    """Apply a §<from> -> §<to> rewrite to `text`.

    `shift_map` is a {from_id: to_id} mapping built from the
    changeReport's `shifted` array. Returns (new_text, count) where
    count is the number of references rewritten.

    Two-pass with sentinels handles chained collisions: when a
    cascade shifts `C.1 -> D.1` and `D.1 -> E.1`, naive sequential
    replacement would rewrite `§C.1` to `§D.1` then to `§E.1`. The
    sentinel pass replaces all matches with unique tokens first,
    then resolves tokens to final values.

    Fenced code blocks are masked before the rewrite and restored
    after -- quoted source / examples stay byte-identical.

    `shift_map` may include identity entries (from == to); those
    are filtered out before processing so the regex doesn't waste
    work on them.
    """
    if not shift_map:
        return text, 0

    # Filter identity entries -- a shift map computed by some other
    # code path may include "no-op" rows. Pass-through cleanly.
    real_shifts = {k: v for k, v in shift_map.items() if k != v}
    if not real_shifts:
        return text, 0

    masked, fence_originals = _mask_fences(text)

    # Pass 1: replace every §<id> match whose id is in shift_map with
    # a unique sentinel. Build a parallel list of replacements indexed
    # by sentinel number.
    replacements: list[str] = []

    def _to_sentinel(m: re.Match[str]) -> str:
        ref_id = m.group("id")
        if ref_id not in real_shifts:
            return m.group(0)  # not shifted, leave alone
        idx = len(replacements)
        replacements.append(f"§{real_shifts[ref_id]}")
        return f"\x00MDH_REF_{idx}\x00"

    sentinel_text = _REF_PATTERN.sub(_to_sentinel, masked)

    if not replacements:
        # Nothing matched -- skip pass 2, return original masked text
        # restored (no work needed).
        return _unmask_fences(sentinel_text, fence_originals), 0

    # Pass 2: replace sentinels with their final §<new> values.
    for idx, value in enumerate(replacements):
        sentinel_text = sentinel_text.replace(
            f"\x00MDH_REF_{idx}\x00", value,
        )

    return _unmask_fences(sentinel_text, fence_originals), len(replacements)


def apply_reference_rewrites(
    conn: sqlite3.Connection,
    shifted: list[dict[str, Any]],
    *,
    by: str | None = None,
) -> dict[str, Any]:
    """Walk every section's body and rewrite §-refs per the shift map.

    Returns a summary dict:
        {
            "rewriteCount": <total refs rewritten across all sections>,
            "sectionsTouched": <count of sections whose body changed>,
        }

    If `shifted` is empty or contains only identity rows, returns
    zero counts and does not touch the DB. Callers can omit the
    summary from the response when both counts are zero.

    Updates go through update_section_content so word_count + audit
    fields stay consistent. `by` defaults to "ref-rewrite" so the
    revision history makes the source obvious.
    """
    if not shifted:
        return {"rewriteCount": 0, "sectionsTouched": 0}

    shift_map = {item["from"]: item["to"] for item in shifted
                 if item.get("from") and item.get("to")}
    if not shift_map:
        return {"rewriteCount": 0, "sectionsTouched": 0}

    total_rewrites = 0
    sections_touched = 0
    timestamp = now()
    actor = by or "ref-rewrite"

    for sec in fetch_all_sections_in_order(conn):
        body = sec.content or ""
        if not body:
            continue
        new_body, count = rewrite_section_references(body, shift_map)
        if count == 0 or new_body == body:
            continue
        update_section_content(
            conn,
            sec.id,
            content=new_body,
            updated_by=actor,
            session_id=None,
            now=timestamp,
        )
        total_rewrites += count
        sections_touched += 1

    return {
        "rewriteCount": total_rewrites,
        "sectionsTouched": sections_touched,
    }


def compute_change_report(
    before: list[OutlineEntry],
    after: list[OutlineEntry],
    filename: str,
    *,
    conn: sqlite3.Connection | None = None,
    by: str | None = None,
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

    # Auto-rewrite §-references across every section's body when
    # sections shifted. The conn parameter is optional for backward
    # compat with callers that haven't been updated yet (or unit
    # tests that diff snapshots without a DB), but every real
    # mutation command should pass it. The rewrite is a no-op when
    # `shifted` is empty.
    ref_rewrite: dict[str, Any] | None = None
    if conn is not None and shifted:
        rw = apply_reference_rewrites(conn, shifted, by=by)
        if rw["rewriteCount"] > 0:
            change_report["referenceRewrites"] = rw
            ref_rewrite = rw

    user_summary = _render_user_summary(
        filename, inserted, deleted, shifted, modified,
        ref_rewrite=ref_rewrite,
    )
    # NB: the full post-mutation outline used to be returned here in
    # `outline`. It was dropped because (a) on docs with hundreds of
    # sections it blew past tool-output caps and got truncated,
    # (b) the changeReport already tells the agent what shifted, and
    # (c) the agent can call markdown_outline explicitly when they
    # need to re-plan against the post-state.
    return {
        "changeReport": change_report,
        "userSummary": user_summary,
    }


def _render_user_summary(
    filename: str,
    inserted: list[dict[str, Any]],
    deleted: list[dict[str, Any]],
    shifted: list[dict[str, Any]],
    modified: list[dict[str, Any]],
    *,
    ref_rewrite: dict[str, Any] | None = None,
) -> str:
    """Render the changeReport as a multi-line human-readable string.

    Format:
      <filename>:
        + <sn> "<title>"
        - <sn> "<title>"
        ~ <from> -> <to> "<title>"
        * <sn>  (+N words)
        > N section references auto-updated across M sections

    The trailing `>` line is added when ref_rewrite is non-empty
    (i.e. one or more body cross-references were rewritten to track
    a section shift). Agents should treat it as "section references
    in any other section's body were updated to match the new
    addresses; you do not need to touch them".

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

    if ref_rewrite and ref_rewrite.get("rewriteCount", 0) > 0:
        any_change = True
        rc = ref_rewrite["rewriteCount"]
        st = ref_rewrite["sectionsTouched"]
        ref_word = "reference" if rc == 1 else "references"
        sec_word = "section" if st == 1 else "sections"
        lines.append(
            f'  > {rc} \u00a7-{ref_word} auto-updated across {st} {sec_word}'
        )

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
    "rewrite_section_references",
    "apply_reference_rewrites",
    "collect_state_counts_from_workspace",
    "now",
    "file_db_path",
    "find_helper_root",
    "META_FOREIGN",
    "PSEUDO_STATES",
    "STATE_LOADED",
    "STATE_READONLY",
]
