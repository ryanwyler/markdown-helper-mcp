"""Tiny shared utilities."""

from __future__ import annotations

import time


def now() -> int:
    """Current epoch seconds (integer). Used for all updated_at /
    created_at / started_at / ended_at timestamps."""
    return int(time.time())


def count_words(s: str | None) -> int:
    if not s:
        return 0
    return len(s.split())


def maybe_unescape_literal_newlines(content: str | None) -> tuple[str | None, bool]:
    """Recover from agents that double-encode markdown content as JSON.

    Some MCP clients deliver content where the agent's `\\n` JSON escape
    didn't get unescaped, so the helper sees the literal two-character
    sequence `\\` + `n` instead of a real newline (byte 0x0A). The
    CommonMark parser then sees `## Foo` mid-paragraph and treats it as
    plain text -- subtree mode silently fails to split into sections.

    Heuristic: if `content` has ZERO real newlines AND at least one
    literal `\\n`, it's almost certainly JSON-double-encoded. Unescape:

      \\n  -> newline
      \\r  -> carriage return
      \\t  -> tab
      \\\\ -> single backslash

    Other backslash sequences (e.g. `\\d` from regex, `\\s`, `\\$`) are
    left exactly as the agent wrote them -- we only handle the common
    escapes.

    The "zero real newlines" guard is the safety net: if the agent put
    even one real newline in their content, we trust them and don't
    touch a thing. Only the unambiguous all-escaped case triggers.

    Returns (content_maybe_unescaped, was_unescaped).
    """
    if not content:
        return content, False
    if "\n" in content:
        # Has at least one real newline -- treat as already-correct.
        return content, False
    if "\\n" not in content:
        # No literal escape either; nothing to do.
        return content, False
    # Unambiguous case: only literal escapes, no real newlines.
    out: list[str] = []
    i = 0
    n = len(content)
    while i < n:
        ch = content[i]
        if ch == "\\" and i + 1 < n:
            nxt = content[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "r":
                out.append("\r")
                i += 2
                continue
            if nxt == "t":
                out.append("\t")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
            # Unknown escape: leave both characters as-is.
        out.append(ch)
        i += 1
    return "".join(out), True
