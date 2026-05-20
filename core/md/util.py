"""Tiny shared utilities."""

from __future__ import annotations

import json
import time


def now() -> int:
    """Current epoch seconds (integer). Used for all updated_at /
    created_at / started_at / ended_at timestamps."""
    return int(time.time())


def count_words(s: str | None) -> int:
    if not s:
        return 0
    return len(s.split())


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

MAX_TAG_LENGTH = 50
_TAG_BAD_CHARS = set(" \t\n\r")


def normalize_tags(tags) -> list[str]:
    """Validate and normalize a list of tags. Returns a sorted, de-
    duplicated list of lowercase tag strings.

    Rules:
      - None / empty list -> [] (clears tags).
      - Each tag must be a non-empty string.
      - Whitespace inside a tag is rejected (tags are identifiers, not
        free-form notes).
      - Tags longer than MAX_TAG_LENGTH are rejected.
      - Tags are lowercased so "Billing" and "billing" collapse.
      - Order is normalized to sorted ascending (tags are a SET; agents
        shouldn't have to think about order when comparing).
      - Duplicates are silently collapsed.

    Replace semantics (consistent with how content/state work): passing
    `tags=[...]` always REPLACES the section's full tag list. To merge,
    callers must read existing tags first, union, then write.

    Raises ValueError on any validation failure with a message naming
    the offending tag.
    """
    if tags is None:
        return []
    if not isinstance(tags, list):
        raise ValueError(f"tags must be a list, got {type(tags).__name__}")
    seen: set[str] = set()
    for t in tags:
        if not isinstance(t, str):
            raise ValueError(
                f"tags must be strings, got {type(t).__name__}: {t!r}"
            )
        if not t:
            raise ValueError("tags may not be empty strings")
        if any(ch in _TAG_BAD_CHARS for ch in t):
            raise ValueError(
                f"tag {t!r} contains whitespace; tags are identifiers, "
                f"not free-form notes"
            )
        if len(t) > MAX_TAG_LENGTH:
            raise ValueError(
                f"tag {t!r} exceeds {MAX_TAG_LENGTH} characters"
            )
        seen.add(t.lower())
    return sorted(seen)


def tags_to_json(tags: list[str]) -> str | None:
    """Serialize a tag list to the storage form (JSON text). Returns
    None for the empty case so the column stores NULL instead of '[]'.
    """
    if not tags:
        return None
    return json.dumps(tags)


def tags_from_json(text) -> list[str]:
    """Deserialize from storage. Tolerant of None / empty string /
    '[]' / malformed JSON (returns [] in degenerate cases)."""
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    # Trust the writer (normalize_tags) ran, but defensively drop any
    # non-string entries that snuck in.
    return [t for t in parsed if isinstance(t, str)]


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
