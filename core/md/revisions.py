"""Revision history table operations.

Every section update appends a row here so the rejection-revision flow
can show the prior content + reviewer notes.
"""

from __future__ import annotations

import sqlite3

from .util import now


def record_revision(
    conn: sqlite3.Connection,
    *,
    section_id: str,
    content: str | None,
    state: str,
    notes: str | None,
    by: str,
) -> None:
    """Append a revision row for a section.

    Notes are typically:
      - None for ordinary writes
      - The rejection text on rejected revisions
      - "ingested via open" when sections are first imported from disk
    """
    conn.execute(
        """INSERT INTO revisions(section_id, content, state, notes,
                                 created_at, created_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (section_id, content, state, notes, now(), by),
    )


def fetch_recent_revisions(
    conn: sqlite3.Connection, section_id: str, *, limit: int = 10,
) -> list[dict]:
    """Last N revisions for a section, newest first."""
    rows = conn.execute(
        "SELECT state, notes, created_at, created_by "
        "FROM revisions WHERE section_id=? "
        "ORDER BY id DESC LIMIT ?",
        (section_id, limit),
    ).fetchall()
    return [
        {
            "state": r["state"],
            "notes": r["notes"],
            "createdAt": r["created_at"],
            "createdBy": r["created_by"],
        }
        for r in rows
    ]
