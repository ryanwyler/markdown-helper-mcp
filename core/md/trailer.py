"""Trailer block: per-section state preservation in the saved markdown.

THE PROBLEM
===========

The workspace cache (.markdown-helper/<dir>/<filename>/db.sqlite3)
holds per-section state, seeds, tags, and the audit history. The .md
file on disk holds the rendered prose. If the cache is dropped --
session compaction, fresh-cwd open, another agent picks up the doc
weeks later -- the workspace metadata is gone. Open re-ingests the
file and marks every section `loaded`. State / seeds / tags / archived
subtrees: lost.

The trailer makes the .md file self-contained. At save time we append
an HTML comment block at the end of the file (invisible in rendered
markdown) carrying the per-section state worth round-tripping. At
open time we detect the block, parse it, and apply the metadata over
the freshly-ingested workspace.

FORMAT
======

A single HTML comment at the very end of the file, preceded by a
blank line:

    <!-- markdown-helper:v1
    {
      "v": 1,
      "sections": {
        "A.1": {"state": "in-progress", "seed": "...", "tags": [...]},
        "B.2": {"state": "blocked"}
      },
      "archived": [
        {
          "parent": "A",          // parent sectionNumber in the visible
                                  // (post-archive) doc, or null for top-level
          "title": "Old approach",
          "body": "...",
          "state": "archived",
          "seed": "...",
          "tags": [...],
          "children": [{"title": "...", "body": "...", ...}]
        }
      ]
    }
    -->

ADDRESSING
==========

Sections are keyed by `sectionNumber` -- the agent-visible address
(A, A.1.2, B, ...). This matches markdown's natural document
structure: a section is identified by its position in the heading
tree, the same way a legal document numbers its clauses.

SectionNumbers are positional and SHIFT under structural mutations,
but the trailer reflects a SNAPSHOT at save time. Save renders the
file AND writes the trailer in one operation -- the addresses on
both sides of the round-trip refer to the same structure. Drift can
only happen if someone hand-edits the .md between save and the next
open, in which case the open path's title check catches the drift
and emits a `trailerOrphans` warning.

ARCHIVED SECTIONS
=================

Sections in the archived state are NOT rendered in the visible
markdown. They live entirely in the trailer's `archived` array.
Each archived entry records its parent's sectionNumber (in the
VISIBLE post-archive doc), so the open path can attach the subtree
back under the right parent. Archived subtrees are inserted at the
END of their parent's child chain on re-open -- precise position
relative to visible siblings is NOT preserved (archived sections are
out-of-flow by definition).

WHAT GETS WRITTEN
=================

Only "non-default" data, to keep trailers small and diffs readable:

- `sections[sn]` is written only when at least ONE of {state != initial
   AND != 'loaded'}, seed, tags is set.
- `archived` is the parallel structure for sections in the archived
   terminal state (or any state with archived semantics in the doc's
   schema). Archived sections are NOT rendered in the visible markdown
   at all -- they live only in the trailer.

A doc with no agent-set state / seeds / tags / archived subtrees
ends up with NO trailer block. Save is a pure markdown writer in
that case.

WHAT GETS APPLIED ON OPEN
=========================

For each entry in `sections`:
  - Look up the section by sectionNumber in the just-ingested workspace.
  - If the section's title differs from the trailer's stored `title`,
    treat as orphan (the visible doc has been edited; the address no
    longer means what the trailer thought).
  - If matched, write state/seed/tags onto the section. The section's
    `loaded` state from ingest is overwritten by the trailer's state.

For each entry in `archived`:
  - Re-insert the subtree at the named `position` (after:<sn> or
    under:<sn> or top:<index>) with the recorded title/body/state/
    seed/tags/children.

Orphans (trailer entries that couldn't be matched) are returned to
the agent in a `trailerOrphans` array so they can decide whether to
re-attach by title-match, archive, or accept the loss.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Sentinel and pattern for the trailer. The header is rigid so the
# matcher can find it reliably; the inner JSON is free-form (we don't
# parse it line-by-line, we slurp it).
TRAILER_BEGIN = "<!-- markdown-helper:v1"
TRAILER_END = "-->"
# Trailing whitespace pattern after the closing `-->`. The trailer
# must terminate at end-of-file (modulo trailing whitespace/newlines).
_TRAILER_TAIL_RE = re.compile(r"\n-->\s*\Z", re.DOTALL)


def strip_trailer(text: str) -> tuple[str, str | None]:
    """If `text` ends with a trailer block, return (text-without-trailer,
    trailer-json-string). Otherwise (text, None).

    The trailer is removed cleanly including any blank lines between
    the document body and the comment opener.

    Implementation note: we look for the LAST `<!-- markdown-helper:v1`
    in the text, then verify the closing `-->` is at end-of-file.
    Using a regex that scans from the start can match an EARLIER
    trailer-shaped block (e.g., a code-fenced example INSIDE the
    document body) and consume content all the way to the real
    trailer's `-->`, destroying the document. Scanning from the end
    is unambiguous: there's only one "last opener" and it's either
    the real trailer or there isn't one.
    """
    # Find the rightmost opener.
    start = text.rfind(TRAILER_BEGIN)
    if start < 0:
        return text, None
    # The opener should be at the start of a line (preceded by \n
    # or be at index 0). Otherwise this is a false-positive inside
    # some other text.
    if start > 0 and text[start - 1] != "\n":
        return text, None
    # Verify the closing `-->` is at end-of-file (modulo whitespace).
    tail_m = _TRAILER_TAIL_RE.search(text, start)
    if tail_m is None:
        return text, None
    # Extract the JSON body between the opener line and the closing
    # `-->` line. The opener line itself is consumed (its trailing
    # newline marks the start of the body).
    body_start = text.find("\n", start) + 1
    body_end = tail_m.start() + 1  # +1 to skip the leading \n in `\n-->`
    body = text[body_start:body_end].strip()
    head = text[:start].rstrip("\n") + "\n"
    return head, body


def parse_trailer(trailer_json: str) -> dict[str, Any]:
    """Parse a trailer's JSON body, returning a dict with `sections`,
    `archived`, and `schema` keys.

    Returns an empty payload (sections={}, archived=[], schema=None)
    when the JSON is malformed or doesn't have the expected shape.
    Missing inner keys are individually defaulted so callers can
    iterate without branching.
    """
    empty = {"sections": {}, "archived": [], "schema": None}
    try:
        data = json.loads(trailer_json)
    except (ValueError, TypeError):
        return empty
    if not isinstance(data, dict) or data.get("v") != 1:
        return empty
    sections = data.get("sections")
    if not isinstance(sections, dict):
        sections = {}
    archived = data.get("archived")
    if not isinstance(archived, list):
        archived = []
    schema = data.get("schema")
    if not isinstance(schema, list):
        schema = None
    return {"sections": sections, "archived": archived, "schema": schema}


def render_trailer(
    sections: dict, archived: list, *,
    schema=None,
    schema_is_default: bool = True,
) -> str:
    """Render the trailer block (including HTML comment wrapper).

    Returns the empty string when there's nothing worth recording --
    a doc with no agent-set state / seeds / tags / archived subtrees
    AND a default schema ends up with no trailer at all (clean diffs).

    Includes the schema in the trailer ONLY when the doc declares a
    non-default schema. A reopen needs the schema to make sense of the
    per-section state names (`archived`, `in-progress`, etc.).
    """
    has_payload = bool(sections or archived)
    has_custom_schema = schema is not None and not schema_is_default

    if not has_payload and not has_custom_schema:
        return ""
    body: dict[str, Any] = {"v": 1}
    if has_custom_schema and schema is not None:
        body["schema"] = _serialize_schema(schema)
    if sections:
        body["sections"] = sections
    if archived:
        body["archived"] = archived
    # Pretty-print so a human reading the .md can scan the trailer.
    json_text = json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False)
    return f"\n\n{TRAILER_BEGIN}\n{json_text}\n{TRAILER_END}\n"


def _serialize_schema(schema) -> list:
    """Serialize a Schema to the same JSON-friendly form the agent
    would pass to markdown_open --states-json. List of objects with
    the role flags set."""
    out = []
    for s in getattr(schema, "states", []):
        entry = {"name": s.name}
        for flag in ("initial", "working", "terminal", "needsAttention", "archived"):
            if getattr(s, flag, False):
                entry[flag] = True
        out.append(entry)
    return out


def _is_default_schema(schema) -> bool:
    """True iff schema is the default [pending, done]."""
    try:
        names = list(schema.names())
    except Exception:
        return False
    return names == ["pending", "done"]


def section_metadata_for_trailer(
    section_number: str,
    title: str | None,
    state: str,
    seed: str | None,
    tags: list[str],
    *,
    schema,
    initial_state: str,
) -> dict[str, Any] | None:
    """Decide whether to write a trailer row for this section, and if
    so, what to include. Returns None when there's nothing worth
    persisting (state is initial-or-loaded, no seed, no tags).

    `title` is included in every written entry so the open path can
    sanity-check that the section at this address has the same title
    (and emit a trailerOrphan warning if not).
    """
    # State worth recording: anything that ISN'T the initial state and
    # ISN'T the `loaded` pseudo-state. Terminal states (done, archived,
    # rejected, etc.) and intermediate states (in-progress, blocked)
    # are all worth recording. `loaded` is the post-ingest sentinel
    # and isn't meaningful to persist.
    nontrivial_state = state != "loaded" and state != initial_state
    has_seed = bool(seed)
    has_tags = bool(tags)
    if not (nontrivial_state or has_seed or has_tags):
        return None
    out: dict[str, Any] = {"title": title}
    if nontrivial_state:
        out["state"] = state
    if has_seed:
        out["seed"] = seed
    if has_tags:
        out["tags"] = list(tags)
    return out


def archived_state_names(schema) -> set[str]:
    """States in the schema whose `archived` flag is set, OR the
    literal name 'archived' if the schema declares it. The archive
    behavior is: such sections are hidden from rendered markdown and
    live only in the trailer.

    We treat any state literally named `archived` as archive-flavor
    regardless of schema flags -- so the agent's natural choice "let's
    add an archived state" Just Works.
    """
    names: set[str] = set()
    for state_def in getattr(schema, "states", []):
        if getattr(state_def, "archived", False):
            names.add(state_def.name)
        if state_def.name == "archived":
            names.add(state_def.name)
    return names


# ---------------------------------------------------------------------------
# Build trailer from workspace state
# ---------------------------------------------------------------------------

def collect_trailer_data(
    conn,
    *,
    schema,
    initial_state: str,
) -> tuple[dict, list, set[str]]:
    """Walk the workspace and produce the trailer's `sections` and
    `archived` data structures, plus the set of UUIDs that should be
    excluded from the rendered markdown.

    Returns (sections_dict, archived_list, skip_uuids).

    sections_dict: { sectionNumber: {title, state?, seed?, tags?} }
      One entry per non-archived section that has non-default state,
      seed, or tags. Title is always recorded so open can sanity-check
      that the address still refers to the same section.

    archived_list: [ {parent, title, body, state, seed, tags, children} ]
      One entry per archived TOP-OF-SUBTREE (descendants of an
      archived section are folded into `children`, not exploded into
      separate entries). `parent` is the sectionNumber the section
      should attach under WHEN THE FILE IS RE-OPENED (i.e. computed
      against the post-archive visible doc), or null for top-level.

    skip_uuids: set of UUIDs the renderer should drop entirely. Equal
      to the union of all archived subtrees' UUIDs.
    """
    from .sections import fetch_children
    from .ids import compute_section_number

    arch_states = archived_state_names(schema)

    # Pass 1: collect archived subtrees by walking from the roots.
    # When we hit an archived section, we record its subtree and DO
    # NOT recurse into its visible-rendering walk (descendants of an
    # archived section are also hidden, naturally).
    skip_uuids: set[str] = set()
    archived_entries: list[dict] = []
    sections_dict: dict[str, dict] = {}

    def gather_subtree(sec) -> dict:
        """Recursively turn a workspace section into the archived-entry
        body shape: {title, body, state, seed, tags, children}."""
        kids = []
        for child in fetch_children(conn, sec.id):
            skip_uuids.add(child.id)
            kids.append(gather_subtree(child))
        entry: dict = {
            "title": sec.title,
            "body": sec.content or "",
            "state": sec.state,
        }
        if sec.seed:
            entry["seed"] = sec.seed
        if sec.tags:
            entry["tags"] = list(sec.tags)
        if kids:
            entry["children"] = kids
        return entry

    def walk_visible(parent_id, visible_parent_sn):
        """Walk children of parent_id. Among them, route each into one
        of three buckets:
          - archived: build subtree + record parent_sn, add to skip set
          - non-archived: record per-section overrides if any, recurse
            into its children for further routing.

        `visible_parent_sn` is the parent's sectionNumber AS IT WILL
        APPEAR IN THE POST-ARCHIVE VISIBLE DOC. It's `None` at the
        forest root. For nested calls we pass the child's POST-ARCHIVE
        sectionNumber, which we compute on the fly.

        Compute the visible siblings list first so we can assign post-
        archive numbers.
        """
        children = fetch_children(conn, parent_id)
        # Letter/number index for VISIBLE siblings only.
        visible_idx = 0  # count of visible titled siblings under parent
        for child in children:
            if child.state in arch_states:
                # Archived: record + skip + don't compute visible sn.
                skip_uuids.add(child.id)
                entry = gather_subtree(child)
                entry["parent"] = visible_parent_sn
                archived_entries.append(entry)
                continue
            # Visible: compute its post-archive sectionNumber.
            if visible_parent_sn is None:
                # Top-level: letter (A, B, ...) based on visible_idx.
                # Use the existing letter-for-index helper for symmetry.
                from .ids import _letter_for_index
                if child.title is None:
                    # Headless preamble -> "0"
                    child_sn = "0"
                else:
                    child_sn = _letter_for_index(visible_idx)
                    visible_idx += 1
            else:
                # Nested: visible_idx (1-based) suffix on parent_sn.
                visible_idx += 1
                child_sn = f"{visible_parent_sn}.{visible_idx}"

            # Record per-section overrides if any.
            meta = section_metadata_for_trailer(
                section_number=child_sn,
                title=child.title,
                state=child.state,
                seed=child.seed,
                tags=child.tags,
                schema=schema,
                initial_state=initial_state,
            )
            if meta is not None:
                sections_dict[child_sn] = meta
            # Recurse into the child's visible subtree.
            walk_visible(child.id, child_sn)

    walk_visible(None, None)
    return sections_dict, archived_entries, skip_uuids


# ---------------------------------------------------------------------------
# Apply trailer to workspace after fresh ingest
# ---------------------------------------------------------------------------

def apply_trailer(
    conn,
    parsed_trailer: dict,
    *,
    schema,
    now_ts: int,
) -> list[dict]:
    """After the visible doc has been ingested into a fresh workspace,
    apply the trailer's per-section overrides + re-insert archived
    subtrees. Returns a list of orphan records (trailer entries that
    couldn't be matched).

    Each orphan record: {sectionNumber, title, droppedMetadata: {...}}
    """
    from .sections import (
        fetch_all_sections_in_order, fetch_children, append_child,
    )
    from .ids import compute_section_number, resolve_section_number
    from .util import tags_to_json

    orphans: list[dict] = []

    # Pass 1: per-section overrides.
    # Build a sectionNumber -> uuid map by walking the freshly-ingested
    # workspace once.
    sn_to_uuid: dict[str, str] = {}
    sn_to_title: dict[str, str | None] = {}
    for sec in fetch_all_sections_in_order(conn):
        sn = compute_section_number(conn, sec.id).display
        sn_to_uuid[sn] = sec.id
        sn_to_title[sn] = sec.title

    for sn, meta in (parsed_trailer.get("sections") or {}).items():
        if not isinstance(meta, dict):
            continue
        expected_title = meta.get("title")
        uuid = sn_to_uuid.get(sn)
        actual_title = sn_to_title.get(sn) if uuid else None
        if uuid is None or actual_title != expected_title:
            # Section moved or title changed -> orphan.
            dropped = {k: v for k, v in meta.items() if k != "title"}
            if dropped:
                orphans.append({
                    "sectionNumber": sn,
                    "title": expected_title,
                    "droppedMetadata": dropped,
                    "reason": "section not found at this address"
                              if uuid is None
                              else f"title differs (doc has {actual_title!r})",
                })
            continue
        # Apply the override.
        new_state = meta.get("state")
        new_seed = meta.get("seed")
        new_tags = meta.get("tags")
        set_clauses: list[str] = ["updated_at=?", "updated_by=?"]
        params: list = [now_ts, "trailer"]
        if new_state:
            set_clauses.append("state=?")
            params.append(new_state)
        if new_seed:
            set_clauses.append("seed=?")
            params.append(new_seed)
        if new_tags is not None:
            set_clauses.append("tags=?")
            params.append(tags_to_json(new_tags) if new_tags else None)
        params.append(uuid)
        conn.execute(
            f"UPDATE sections SET {', '.join(set_clauses)} WHERE id=?",
            params,
        )

    # Pass 2: re-insert archived subtrees.
    def insert_subtree(parent_uuid: str | None, entry: dict) -> None:
        """Append `entry` as the last child of parent_uuid, then
        recurse for its children."""
        title = entry.get("title")
        body = entry.get("body") or ""
        state = entry.get("state") or "archived"
        seed = entry.get("seed")
        tags = entry.get("tags") or []
        inserted = append_child(
            conn,
            parent_id=parent_uuid,
            title=title,
            seed=seed,
            content=body if body else None,
            state=state,
            updated_by="trailer",
            now=now_ts,
            tags=tags or None,
        )
        for child in entry.get("children") or []:
            insert_subtree(inserted.id, child)

    for entry in parsed_trailer.get("archived") or []:
        if not isinstance(entry, dict):
            continue
        parent_sn = entry.get("parent")
        if parent_sn is None:
            parent_uuid = None
        else:
            parent_uuid = sn_to_uuid.get(parent_sn)
            if parent_uuid is None:
                # Parent missing -> orphan the whole archived subtree.
                orphans.append({
                    "title": entry.get("title"),
                    "droppedMetadata": {
                        k: v for k, v in entry.items()
                        if k not in ("parent",)
                    },
                    "reason": f"archived subtree's parent {parent_sn!r} "
                              f"not found in visible doc",
                })
                continue
        insert_subtree(parent_uuid, entry)

    return orphans
