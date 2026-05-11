"""Sections -> markdown text.

Walks the section forest and emits markdown. Heading levels are
COMPUTED from depth in the tree, NOT stored. A section at depth 1 gets
`# Title`, depth 2 gets `## Title`, etc. (capped at depth 6 for
markdown's H6 limit).

Sections with title IS NULL emit no heading line -- just their content.
This is how the "preamble" (text before the first heading) round-trips.
"""

from __future__ import annotations

import sqlite3

from .sections import fetch_children


# Markdown caps at 6 heading levels. Anything deeper renders as h6.
MAX_HEADING_LEVEL = 6


def render_document(conn: sqlite3.Connection) -> str:
    """Concatenate all sections into a single markdown document.

    Pre-order DFS through the forest: each top-level section first
    (followed by its descendants), then the next top-level section.
    Heading levels are computed from depth.

    Sections are joined with a single blank line between them, matching
    the conventional `# Heading\\n\\nbody\\n\\n## Next heading` style.
    """
    blocks: list[str] = []
    _render_into(conn, blocks, parent_id=None, depth=1)
    return "\n\n".join(blocks).rstrip() + "\n"


def _render_into(
    conn: sqlite3.Connection,
    blocks: list[str],
    *,
    parent_id: str | None,
    depth: int,
) -> None:
    """Walk children of `parent_id` in chain order; emit each as
    one or more blocks; recurse into descendants."""
    for sec in fetch_children(conn, parent_id):
        body = (sec.content or "").rstrip()
        if sec.title is None:
            # Headless preamble section: just emit the body.
            if body:
                blocks.append(body)
        else:
            heading_depth = min(MAX_HEADING_LEVEL, depth)
            heading_line = "#" * heading_depth + " " + sec.title
            blocks.append(heading_line)
            if body:
                blocks.append(body)
        # Recurse: this section's children render at depth+1.
        _render_into(conn, blocks, parent_id=sec.id, depth=depth + 1)
