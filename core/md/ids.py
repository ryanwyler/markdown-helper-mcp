"""Section addressing: UUIDs internal, sectionNumber external.

The agent never sees UUIDs. They use `sectionNumber` for everything.
sectionNumber is computed at every operation by walking the linked-list
chains; it is NEVER stored.

Forest model
============

A document is a forest of trees:

  - "Section 0" -- one optional headless top-level section holding
    preamble prose before any heading. Address: "0".
  - "Tree roots" -- top-level sections WITH a title. Each is the root
    of a tree. Addresses are letters: "A", "B", "C", ... (in order).
  - Within each tree, child sections are numbered: "A.1", "A.1.1",
    "A.2", "B.1", etc.

Examples:

    (preamble prose)            -> "0"
    # First heading             -> "A"
    ## Sub                      -> "A.1"
    ### Subsub                  -> "A.1.1"
    # Second heading            -> "B"
    ## Sub of B                 -> "B.1"

Always letter-prefixed for top-level titled sections, even when there's
only one tree. This keeps the addressing rule the same regardless of
how many H1s the doc has.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Optional


def gen_uuid() -> str:
    """New random UUID string (used as section id)."""
    return str(uuid.uuid4())


def gen_job_id() -> str:
    """Short hex job id for dispatch records."""
    return "j" + uuid.uuid4().hex[:11]


# ---------------------------------------------------------------------------
# sectionNumber computation
# ---------------------------------------------------------------------------

@dataclass
class SectionAddress:
    """The agent-visible address of a section.

    `parts` is the list of address segments from root to leaf, e.g.
    ["A", "1", "1"] for "A.1.1", or ["B", "2"] for "B.2", or ["0"] for
    the headless preamble section.

    `display` joins them with dots: "A.1.1", "B.2", "0".
    """
    parts: list[str]

    @property
    def display(self) -> str:
        return ".".join(self.parts)


def compute_section_number(conn: sqlite3.Connection, section_id: str) -> SectionAddress:
    """Compute the current sectionNumber for a given UUID.

    Walks up the parent chain, finding each section's position among its
    siblings (via the prev_sibling_id chain). Top-level sections get
    letter prefixes (A, B, C, ...); the headless preamble section gets
    "0".
    """
    parts: list[str] = []
    current_id: Optional[str] = section_id

    while current_id is not None:
        row = conn.execute(
            "SELECT id, parent_id, prev_sibling_id, title "
            "FROM sections WHERE id=?",
            (current_id,),
        ).fetchone()
        if row is None:
            return SectionAddress(parts=[])

        if row["parent_id"] is None:
            # Top-level section. Two cases: the headless preamble (title
            # IS NULL) gets "0"; titled trees get letter prefixes.
            if row["title"] is None:
                parts.insert(0, "0")
            else:
                # Count titled top-level siblings before this one to get
                # letter index. Walk back along the prev_sibling_id chain
                # only counting rows with a title.
                letter_index = 0
                walker = row["prev_sibling_id"]
                while walker is not None:
                    prev = conn.execute(
                        "SELECT prev_sibling_id, title "
                        "FROM sections WHERE id=?",
                        (walker,),
                    ).fetchone()
                    if prev is None:
                        break
                    if prev["title"] is not None:
                        letter_index += 1
                    walker = prev["prev_sibling_id"]
                parts.insert(0, _letter_for_index(letter_index))
            current_id = None  # done walking
        else:
            # Mid-tree: count siblings before me to get my number among
            # my parent's children.
            position = 1
            walker = row["prev_sibling_id"]
            while walker is not None:
                prev = conn.execute(
                    "SELECT prev_sibling_id FROM sections WHERE id=?",
                    (walker,),
                ).fetchone()
                if prev is None:
                    break
                position += 1
                walker = prev["prev_sibling_id"]
            parts.insert(0, str(position))
            current_id = row["parent_id"]

    return SectionAddress(parts=parts)


def _letter_for_index(idx: int) -> str:
    """0 -> A, 1 -> B, ..., 25 -> Z, 26 -> AA, 27 -> AB, ..."""
    if idx < 0:
        raise ValueError(f"letter index must be >= 0, got {idx}")
    out = ""
    n = idx + 1  # 1-based for base-26
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _index_for_letter(letter: str) -> int:
    """A -> 0, B -> 1, ..., Z -> 25, AA -> 26."""
    letter = letter.upper()
    n = 0
    for ch in letter:
        if not ("A" <= ch <= "Z"):
            raise ValueError(f"invalid letter in address: {letter!r}")
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


# ---------------------------------------------------------------------------
# sectionNumber resolution -- agent gives address, we find UUID
# ---------------------------------------------------------------------------

def resolve_section_number(conn: sqlite3.Connection, section_number: str) -> str | None:
    """Given an agent-supplied sectionNumber (e.g. "A.1.2", "B", "0"),
    find the UUID of that section. Returns None if no section is at
    that address.
    """
    parts = _parse_address(section_number)
    if not parts:
        return None

    head = parts[0]

    # Resolve the top-level section first.
    if head == "0":
        # Headless preamble: top-level with title IS NULL.
        row = conn.execute(
            "SELECT id FROM sections "
            "WHERE parent_id IS NULL AND title IS NULL",
        ).fetchone()
        if row is None:
            return None
        current_id: str = row["id"]
    else:
        # Letter -> nth titled top-level section.
        target_index = _index_for_letter(head)
        # Walk top-level sections in chain order, counting titled ones.
        # Find the head of the top-level chain (parent_id NULL,
        # prev_sibling_id NULL).
        head_row = conn.execute(
            "SELECT id FROM sections "
            "WHERE parent_id IS NULL AND prev_sibling_id IS NULL",
        ).fetchone()
        if head_row is None:
            return None
        # Walk forward from head. Each step: find the row whose
        # prev_sibling_id is my current id.
        idx = -1
        walker_id: str | None = head_row["id"]
        target_uuid: str | None = None
        while walker_id is not None:
            walker = conn.execute(
                "SELECT id, title FROM sections WHERE id=?",
                (walker_id,),
            ).fetchone()
            if walker is None:
                break
            if walker["title"] is not None:
                idx += 1
                if idx == target_index:
                    target_uuid = walker["id"]
                    break
            # Move forward: find row whose prev_sibling_id = walker["id"].
            nxt = conn.execute(
                "SELECT id FROM sections WHERE prev_sibling_id=?",
                (walker["id"],),
            ).fetchone()
            walker_id = nxt["id"] if nxt else None
        if target_uuid is None:
            return None
        current_id = target_uuid

    # Walk down each subsequent numeric segment.
    for seg in parts[1:]:
        try:
            position = int(seg)
        except ValueError:
            return None
        if position < 1:
            return None
        # Find the position'th child of current_id (1-based).
        head_row = conn.execute(
            "SELECT id FROM sections "
            "WHERE parent_id=? AND prev_sibling_id IS NULL",
            (current_id,),
        ).fetchone()
        if head_row is None:
            return None
        child_id: str = head_row["id"]
        for _ in range(position - 1):
            nxt = conn.execute(
                "SELECT id FROM sections WHERE prev_sibling_id=?",
                (child_id,),
            ).fetchone()
            if nxt is None:
                return None
            child_id = nxt["id"]
        current_id = child_id

    return current_id


def _parse_address(address: str) -> list[str]:
    """Split 'A.1.2' into ['A', '1', '2']; 'B' into ['B']; '0' into ['0'].

    Validates that the first segment is either '0' or a letter sequence,
    and subsequent segments are positive integers.
    """
    if not address:
        return []
    parts = address.strip().split(".")
    if not parts[0]:
        return []
    head = parts[0].strip()
    if head == "0":
        if len(parts) > 1:
            raise ValueError("section '0' has no children; bad address")
        return ["0"]
    # Letter sequence
    for ch in head:
        if not ("A" <= ch.upper() <= "Z"):
            raise ValueError(f"top-level address must be a letter or '0', got {head!r}")
    out = [head.upper()]
    for seg in parts[1:]:
        seg = seg.strip()
        try:
            n = int(seg)
        except ValueError:
            raise ValueError(f"address segment must be a number, got {seg!r}")
        if n < 1:
            raise ValueError(f"address segment must be >= 1, got {n}")
        out.append(str(n))
    return out
