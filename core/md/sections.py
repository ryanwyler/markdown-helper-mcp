"""Section CRUD: insert, delete, move, fetch, set, walk.

Sections form a forest of linked lists per parent. Each section has
parent_id (NULL for top-level) and prev_sibling_id (NULL for first in
its sibling chain). To walk a parent's children in order, start from
the first (prev_sibling_id IS NULL) and follow the "row whose
prev_sibling_id = me" chain forward.

Insert/delete/move are all chain-pointer updates -- typically 2-3
SQL writes per operation. No renumbering needed because sectionNumbers
are computed at query time from chain position.

This module is the only place that mutates the `sections` table.
Everything else (cmd modules, dispatch, etc.) goes through these
functions.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from .ids import gen_uuid
from .states import DEFAULT_INITIAL


# ---------------------------------------------------------------------------
# Row shape
# ---------------------------------------------------------------------------

@dataclass
class SectionRow:
    """In-memory copy of a row from the `sections` table."""
    id: str
    parent_id: str | None
    prev_sibling_id: str | None
    title: str | None
    seed: str | None
    content: str | None
    state: str
    word_count: int
    updated_at: int
    updated_by: str | None
    session_id: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SectionRow":
        return cls(
            id=row["id"],
            parent_id=row["parent_id"],
            prev_sibling_id=row["prev_sibling_id"],
            title=row["title"],
            seed=row["seed"],
            content=row["content"],
            state=row["state"],
            word_count=row["word_count"],
            updated_at=row["updated_at"],
            updated_by=row["updated_by"],
            session_id=row["session_id"],
        )


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_section(conn: sqlite3.Connection, section_id: str) -> SectionRow | None:
    row = conn.execute(
        "SELECT * FROM sections WHERE id=?", (section_id,),
    ).fetchone()
    return SectionRow.from_row(row) if row else None


def fetch_children(
    conn: sqlite3.Connection, parent_id: str | None
) -> list[SectionRow]:
    """Return the children of `parent_id` in chain order (first to last).

    `parent_id` IS NULL means top-level sections.
    """
    if parent_id is None:
        head = conn.execute(
            "SELECT * FROM sections "
            "WHERE parent_id IS NULL AND prev_sibling_id IS NULL"
        ).fetchone()
    else:
        head = conn.execute(
            "SELECT * FROM sections "
            "WHERE parent_id=? AND prev_sibling_id IS NULL",
            (parent_id,),
        ).fetchone()
    out: list[SectionRow] = []
    walker = head
    while walker is not None:
        out.append(SectionRow.from_row(walker))
        walker = conn.execute(
            "SELECT * FROM sections WHERE prev_sibling_id=?",
            (walker["id"],),
        ).fetchone()
    return out


def fetch_all_sections_in_order(conn: sqlite3.Connection) -> list[SectionRow]:
    """Pre-order DFS of every section in the doc, in render order.

    Top-level sections come first (in chain order), each followed by all
    its descendants. The result is the order in which sections appear
    in the rendered file.
    """
    result: list[SectionRow] = []

    def walk(parent_id: str | None) -> None:
        for child in fetch_children(conn, parent_id):
            result.append(child)
            walk(child.id)

    walk(None)
    return result


def fetch_outline_tree(conn: sqlite3.Connection) -> list:
    """Build the same `Section` tree shape that `parse_disk_outline`
    returns, populated from the workspace cache.

    Workspace-only fields (uuid, state, seed, session_id) are
    populated; otherwise the shape is identical so range expansion,
    rendering, and serialization treat both sources uniformly.

    Imports `Section` lazily to avoid a circular import with parse.py.
    """
    from .parse import Section
    from .ids import compute_section_number

    def build(parent_id: str | None) -> list:
        out: list = []
        for child in fetch_children(conn, parent_id):
            addr = compute_section_number(conn, child.id).display
            depth = section_depth(conn, child.id)
            sec = Section(
                section_number=addr,
                title=child.title,
                depth=depth,
                body=child.content or "",
                uuid=child.id,
                state=child.state,
                seed=child.seed,
                session_id=child.session_id,
            )
            sec.children = build(child.id)
            out.append(sec)
        return out

    return build(None)


def section_depth(conn: sqlite3.Connection, section_id: str) -> int:
    """Depth of a section in the tree. Top-level = 1; children of
    top-level = 2; etc. The headless preamble section (title IS NULL,
    top-level) is also depth 1, but won't have a heading line rendered
    for it.
    """
    depth = 1
    current_id: Optional[str] = section_id
    while True:
        row = conn.execute(
            "SELECT parent_id FROM sections WHERE id=?",
            (current_id,),
        ).fetchone()
        if row is None or row["parent_id"] is None:
            return depth
        depth += 1
        current_id = row["parent_id"]


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------

def insert_section(
    conn: sqlite3.Connection,
    *,
    parent_id: str | None,
    prev_sibling_id: str | None,
    title: str | None,
    seed: str | None = None,
    content: str | None = None,
    state: str = DEFAULT_INITIAL,
    updated_by: str = "primary",
    now: int,
) -> SectionRow:
    """Insert a new section into a parent's chain.

    The new section's `prev_sibling_id` is set to `prev_sibling_id`.
    If there's a section that previously came after `prev_sibling_id`
    (i.e. its prev_sibling_id was prev_sibling_id), we update IT to
    point at the new section instead.

    Edge cases:
      - prev_sibling_id IS NULL means "insert at the head of the chain":
        the existing head (if any) gets its prev_sibling_id set to the
        new section.
      - parent_id IS NULL means top-level.
    """
    new_id = gen_uuid()
    word_count = _count_words(content) if content else 0

    # Find the row that currently has prev_sibling_id == our new
    # prev_sibling_id (and same parent_id) -- that one becomes our
    # successor, and we need to bump its pointer to us.
    if prev_sibling_id is None:
        if parent_id is None:
            old_head = conn.execute(
                "SELECT id FROM sections "
                "WHERE parent_id IS NULL AND prev_sibling_id IS NULL",
            ).fetchone()
        else:
            old_head = conn.execute(
                "SELECT id FROM sections "
                "WHERE parent_id=? AND prev_sibling_id IS NULL",
                (parent_id,),
            ).fetchone()
        successor_id = old_head["id"] if old_head else None
    else:
        # Successor is whatever row currently has prev_sibling_id =
        # our prev_sibling_id (under the same parent).
        if parent_id is None:
            old_next = conn.execute(
                "SELECT id FROM sections "
                "WHERE parent_id IS NULL AND prev_sibling_id=?",
                (prev_sibling_id,),
            ).fetchone()
        else:
            old_next = conn.execute(
                "SELECT id FROM sections "
                "WHERE parent_id=? AND prev_sibling_id=?",
                (parent_id, prev_sibling_id),
            ).fetchone()
        successor_id = old_next["id"] if old_next else None

    # Insert the new row first.
    conn.execute(
        """INSERT INTO sections(id, parent_id, prev_sibling_id,
                                title, seed, content, state,
                                word_count, updated_at, updated_by,
                                session_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
        (new_id, parent_id, prev_sibling_id,
         title, seed, content, state,
         word_count, now, updated_by),
    )

    # Heal the chain: successor (if any) now points at us.
    if successor_id is not None:
        conn.execute(
            "UPDATE sections SET prev_sibling_id=? WHERE id=?",
            (new_id, successor_id),
        )

    fetched = fetch_section(conn, new_id)
    if fetched is None:
        raise RuntimeError(f"insert_section: row {new_id} not found after INSERT")
    return fetched


def append_child(
    conn: sqlite3.Connection,
    *,
    parent_id: str | None,
    title: str | None,
    seed: str | None = None,
    content: str | None = None,
    state: str = DEFAULT_INITIAL,
    updated_by: str = "primary",
    now: int,
) -> SectionRow:
    """Append as the LAST child of `parent_id` (or last top-level if
    parent_id is None). Convenience over insert_section.
    """
    # Find the current tail of the chain.
    tail_id = _find_tail_of_chain(conn, parent_id)
    return insert_section(
        conn,
        parent_id=parent_id,
        prev_sibling_id=tail_id,
        title=title,
        seed=seed,
        content=content,
        state=state,
        updated_by=updated_by,
        now=now,
    )


def _find_tail_of_chain(
    conn: sqlite3.Connection, parent_id: str | None
) -> str | None:
    """Return the id of the LAST child in `parent_id`'s chain, or None
    if there are no children yet.
    """
    if parent_id is None:
        head = conn.execute(
            "SELECT id FROM sections "
            "WHERE parent_id IS NULL AND prev_sibling_id IS NULL"
        ).fetchone()
    else:
        head = conn.execute(
            "SELECT id FROM sections "
            "WHERE parent_id=? AND prev_sibling_id IS NULL",
            (parent_id,),
        ).fetchone()
    if head is None:
        return None
    walker_id: str = head["id"]
    while True:
        nxt = conn.execute(
            "SELECT id FROM sections WHERE prev_sibling_id=?",
            (walker_id,),
        ).fetchone()
        if nxt is None:
            return walker_id
        walker_id = nxt["id"]


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def delete_section(
    conn: sqlite3.Connection, section_id: str, *, recursive: bool = True
) -> int:
    """Remove a section. Heals the chain so its successor points at its
    predecessor.

    If `recursive=True` (default), descendants are deleted too. If
    `recursive=False` and the section has children, raises ValueError.

    Returns the count of sections deleted.
    """
    sec = fetch_section(conn, section_id)
    if sec is None:
        return 0

    children = fetch_children(conn, section_id)
    if children and not recursive:
        raise ValueError(
            f"section {section_id!r} has {len(children)} children; "
            f"pass recursive=True to delete them too"
        )

    # Heal the chain: anyone pointing at me now points at my predecessor.
    conn.execute(
        "UPDATE sections SET prev_sibling_id=? WHERE prev_sibling_id=?",
        (sec.prev_sibling_id, section_id),
    )

    deleted = 0
    if recursive:
        # DFS delete descendants first (foreign-key-friendly, though we
        # don't enforce FKs here; still good practice).
        for child in children:
            deleted += delete_section(conn, child.id, recursive=True)

    # Remove revisions and assistants too.
    conn.execute("DELETE FROM revisions WHERE section_id=?", (section_id,))
    conn.execute("DELETE FROM assistants WHERE section_id=?", (section_id,))
    conn.execute("DELETE FROM sections WHERE id=?", (section_id,))
    deleted += 1
    return deleted


# ---------------------------------------------------------------------------
# Move
# ---------------------------------------------------------------------------

def move_section(
    conn: sqlite3.Connection,
    section_id: str,
    *,
    new_parent_id: str | None,
    new_prev_sibling_id: str | None,
    now: int,
) -> SectionRow:
    """Move a section to a new position. Heals both the old chain
    (where it used to live) and the new chain (where it goes).

    Validates that the new parent isn't the section itself or one of
    its descendants (would create a cycle).
    """
    sec = fetch_section(conn, section_id)
    if sec is None:
        raise ValueError(f"section {section_id!r} not found")

    # Cycle check: new_parent_id must not be section_id itself or any
    # descendant.
    if new_parent_id == section_id:
        raise ValueError(f"cannot make section {section_id!r} its own parent")
    if new_parent_id is not None:
        ancestor: Optional[str] = new_parent_id
        while ancestor is not None:
            if ancestor == section_id:
                raise ValueError(
                    f"cannot move section {section_id!r} under its own descendant"
                )
            row = conn.execute(
                "SELECT parent_id FROM sections WHERE id=?", (ancestor,),
            ).fetchone()
            ancestor = row["parent_id"] if row else None

    # Heal the OLD chain: my old successor now points at my old predecessor.
    conn.execute(
        "UPDATE sections SET prev_sibling_id=? WHERE prev_sibling_id=?",
        (sec.prev_sibling_id, section_id),
    )

    # In the NEW chain, find the row that currently has
    # prev_sibling_id == new_prev_sibling_id (under new_parent_id) --
    # it's about to become my successor.
    if new_prev_sibling_id is None:
        if new_parent_id is None:
            old_head = conn.execute(
                "SELECT id FROM sections "
                "WHERE parent_id IS NULL AND prev_sibling_id IS NULL "
                "AND id != ?",
                (section_id,),
            ).fetchone()
        else:
            old_head = conn.execute(
                "SELECT id FROM sections "
                "WHERE parent_id=? AND prev_sibling_id IS NULL "
                "AND id != ?",
                (new_parent_id, section_id),
            ).fetchone()
        successor_id = old_head["id"] if old_head else None
    else:
        if new_parent_id is None:
            old_next = conn.execute(
                "SELECT id FROM sections "
                "WHERE parent_id IS NULL AND prev_sibling_id=? "
                "AND id != ?",
                (new_prev_sibling_id, section_id),
            ).fetchone()
        else:
            old_next = conn.execute(
                "SELECT id FROM sections "
                "WHERE parent_id=? AND prev_sibling_id=? "
                "AND id != ?",
                (new_parent_id, new_prev_sibling_id, section_id),
            ).fetchone()
        successor_id = old_next["id"] if old_next else None

    # Update me to point at new parent + new prev.
    conn.execute(
        """UPDATE sections
           SET parent_id=?, prev_sibling_id=?, updated_at=?
           WHERE id=?""",
        (new_parent_id, new_prev_sibling_id, now, section_id),
    )

    # Heal the new chain: successor now points at me.
    if successor_id is not None and successor_id != section_id:
        conn.execute(
            "UPDATE sections SET prev_sibling_id=? WHERE id=?",
            (section_id, successor_id),
        )

    fetched = fetch_section(conn, section_id)
    if fetched is None:
        raise RuntimeError(f"move_section: row {section_id} not found after move")
    return fetched


# ---------------------------------------------------------------------------
# Set (write content / title / state)
# ---------------------------------------------------------------------------

def update_section_content(
    conn: sqlite3.Connection,
    section_id: str,
    *,
    content: str | None,
    title: str | None = None,  # None = leave unchanged; pass empty string to clear
    state: str | None = None,  # None = leave unchanged
    seed: str | None = None,   # None = leave unchanged
    updated_by: str,
    session_id: str | None,
    now: int,
) -> None:
    """Update a section's content (and optionally title/state/seed).

    A None argument means "leave that field unchanged" (COALESCE-style).
    Title is treated specially: pass empty string to clear it (turn the
    section into a headless preamble); pass None to leave unchanged.
    """
    word_count = _count_words(content) if content else 0
    set_clauses = ["content=?", "word_count=?", "updated_at=?", "updated_by=?",
                   "session_id=COALESCE(?, session_id)"]
    params: list = [content, word_count, now, updated_by, session_id]

    if title is not None:
        set_clauses.append("title=?")
        params.append(title if title != "" else None)
    if state is not None:
        set_clauses.append("state=?")
        params.append(state)
    if seed is not None:
        set_clauses.append("seed=?")
        params.append(seed)

    params.append(section_id)
    conn.execute(
        f"UPDATE sections SET {', '.join(set_clauses)} WHERE id=?",
        params,
    )


# ---------------------------------------------------------------------------
# Position resolution: given a target section + position keyword, return
# the (parent_id, prev_sibling_id) pair where a new section should land.
# ---------------------------------------------------------------------------

def position_for_insert(
    conn: sqlite3.Connection,
    *,
    before: str | None = None,
    after: str | None = None,
    under: str | None = None,
    at_top_level: bool = False,
) -> tuple[str | None, str | None]:
    """Resolve `before:` / `after:` / `under:` / top-level placement
    into (parent_id, prev_sibling_id) for an insert.

    - `before: <uuid>` → new section becomes that section's predecessor.
      Inherits its parent. prev_sibling_id is whatever the target's prev
      was; the target's prev gets bumped to the new section by the insert
      operation itself.
    - `after: <uuid>` → new section becomes that section's successor.
      Inherits its parent. prev_sibling_id is the target's id.
    - `under: <uuid>` → append as last child of that section.
    - `at_top_level=True` (with all others None) → append at end of
      top-level chain.

    Exactly one of before/after/under (or at_top_level) must be passed.
    """
    chosen = sum(1 for x in (before, after, under) if x is not None)
    if at_top_level:
        chosen += 1
    if chosen != 1:
        raise ValueError(
            "exactly one of before / after / under (or at_top_level) is required"
        )

    if at_top_level:
        return None, _find_tail_of_chain(conn, None)

    if before is not None:
        target = fetch_section(conn, before)
        if target is None:
            raise ValueError(f"section {before!r} not found (before)")
        return target.parent_id, target.prev_sibling_id

    if after is not None:
        target = fetch_section(conn, after)
        if target is None:
            raise ValueError(f"section {after!r} not found (after)")
        return target.parent_id, target.id

    # under
    if under is not None:
        target = fetch_section(conn, under)
        if target is None:
            raise ValueError(f"section {under!r} not found (under)")
        return target.id, _find_tail_of_chain(conn, target.id)

    # Unreachable.
    raise ValueError("invalid position arguments")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_words(s: str | None) -> int:
    if not s:
        return 0
    return len(s.split())


# ---------------------------------------------------------------------------
# Splice subtree: replace a target section's body AND children with a
# parsed subtree. Used by cmd_set+subtree, cmd_insert+subtree, cmd_patch,
# and reconcile.
# ---------------------------------------------------------------------------

def splice_subtree(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    new_body: str | None,
    new_title: str | None,  # None = leave unchanged; "" = clear
    children_specs: list[dict],
    state: str,
    updated_by: str,
    session_id: str | None,
    now: int,
) -> None:
    """Replace target's body, optional title, and child subtree.

    UUID-PRESERVING: when an existing descendant's title matches a new
    spec's title within the same parent, the UUID is reused (revisions,
    session_ids, dispatched-by survive). Only sections without a
    matching counterpart are deleted; only new specs without a matching
    existing section are inserted.

    `children_specs` is a recursive list of dicts:
      [
        {"title": str|None, "body": str, "children": [...]},
        ...
      ]

    Operations (single transaction at caller):
      1. Update target's body/title/state.
      2. Walk children_specs against existing children, matching by
         title within the same parent. Recurse into matching pairs;
         insert new specs; delete unmatched existing children at the
         end.
    """
    # 1. Update target.
    update_section_content(
        conn, target_id,
        content=new_body,
        title=new_title,
        state=state,
        updated_by=updated_by,
        session_id=session_id,
        now=now,
    )

    # 2. Recurse.
    _splice_children_under(
        conn,
        parent_id=target_id,
        specs=children_specs,
        state=state,
        updated_by=updated_by,
        now=now,
    )


def _splice_children_under(
    conn: sqlite3.Connection,
    *,
    parent_id: str,
    specs: list[dict],
    state: str,
    updated_by: str,
    now: int,
) -> None:
    """Recursive splice: reconcile existing children of `parent_id`
    against `specs`. Match by title; UUIDs survive matches.

    Multiple children with the same title are matched positionally
    (first-spec-with-title-X gets first-existing-with-title-X). Order
    is reset to follow spec order via detach + reattach to the end of
    the parent's chain.
    """
    existing = list(fetch_children(conn, parent_id))

    by_title: dict[str | None, list] = {}
    for sec in existing:
        by_title.setdefault(sec.title, []).append(sec)

    matched_ids: set[str] = set()
    for spec in specs:
        title = spec.get("title")
        body = spec.get("body") or None
        children = spec.get("children") or []
        seed = spec.get("seed")

        candidates = by_title.get(title, [])
        match = candidates.pop(0) if candidates else None

        if match is not None:
            matched_ids.add(match.id)
            update_section_content(
                conn, match.id,
                content=body,
                title=None,  # title already matches
                seed=seed if seed is not None else match.seed,
                state=state,
                updated_by=updated_by,
                session_id=match.session_id,
                now=now,
            )
            # Re-position to end of parent's chain so the final order
            # mirrors spec order.
            move_section(
                conn, match.id,
                new_parent_id=parent_id,
                new_prev_sibling_id=_last_child_id(conn, parent_id, exclude=match.id),
                now=now,
            )
            _splice_children_under(
                conn,
                parent_id=match.id,
                specs=children,
                state=state,
                updated_by=updated_by,
                now=now,
            )
        else:
            new_section = append_child(
                conn,
                parent_id=parent_id,
                title=title,
                seed=seed,
                content=body,
                state=state,
                updated_by=updated_by,
                now=now,
            )
            if children:
                _splice_children_under(
                    conn,
                    parent_id=new_section.id,
                    specs=children,
                    state=state,
                    updated_by=updated_by,
                    now=now,
                )

    # Delete any existing children that weren't matched.
    for sec in existing:
        if sec.id not in matched_ids:
            delete_section(conn, sec.id, recursive=True)


def _last_child_id(
    conn: sqlite3.Connection, parent_id: str, *, exclude: str | None,
) -> str | None:
    """Return the UUID of the last child in parent_id's chain,
    excluding `exclude` if present.
    """
    children = fetch_children(conn, parent_id)
    for sec in reversed(children):
        if sec.id != exclude:
            return sec.id
    return None
