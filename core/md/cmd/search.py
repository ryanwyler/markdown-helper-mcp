"""markdown_search: regex grep over titles and/or bodies in a workspace.

Different shape than markdown_outline + titleContains:
  - Title-only substring is what `titleContains` on outline is for. Use
    that for the 90% case -- it composes with state filters and is
    free.
  - markdown_search is for the heavy case: regex, body matches, returned
    with context lines, across many sections. Different return shape
    (per-match records, not section tree).

Pattern is a Python regex. Match scope can be "title", "body", or
"title,body" (default). Case-insensitive by default (--case-sensitive
to disable). Results are returned in DOCUMENT ORDER -- no relevance
ranking. Documents are read top-to-bottom; matches at the top come
first.
"""

from __future__ import annotations

import argparse
import re

from . import _common as c
from ..parse import flatten_outline
from ..sections import fetch_outline_tree
from ..storage import open_db


# Default context window for body matches: lines before/after the
# matching line that get included in the result. Small enough to be
# token-efficient, large enough to be readable on its own.
DEFAULT_CONTEXT = 2

# Cap on number of matches returned. If exceeded, returns the first N
# plus a `truncated: true` flag + the unreported count. Search across
# 400-section docs should never blow past tool-output limits.
MAX_MATCHES = 200


def register_parser(sub) -> None:
    p = sub.add_parser("search",
                       help="regex grep over section titles and/or bodies")
    p.add_argument("--filename", required=True)
    p.add_argument("--pattern", required=True,
                   help="Python regex. Use scope to restrict to title, "
                        "body, or both (default).")
    p.add_argument("--scope", default="title,body",
                   help="comma-separated list of 'title', 'body'. "
                        "Default 'title,body' (search both).")
    p.add_argument("--case-sensitive", action="store_true",
                   help="match case-sensitively (default is case-"
                        "insensitive).")
    p.add_argument("--context", type=int, default=DEFAULT_CONTEXT,
                   help=f"lines of body context around each body match "
                        f"(default {DEFAULT_CONTEXT}).")
    p.add_argument("--limit", type=int, default=MAX_MATCHES,
                   help=f"cap on number of returned matches "
                        f"(default {MAX_MATCHES}).")
    c.add_pretty(p)
    p.set_defaults(func=cmd_search)


def cmd_search(args: argparse.Namespace) -> int:
    resolved = c.resolve_workspace_or_die(args)
    if resolved is None:
        return 1
    _, ws_dir, filename = resolved

    # Parse scope.
    scope_parts = {s.strip().lower() for s in args.scope.split(",") if s.strip()}
    if not scope_parts:
        c.emit_error({"error": "scope must include at least one of: title, body"})
        return 2
    invalid = scope_parts - {"title", "body"}
    if invalid:
        c.emit_error({
            "error": f"unknown scope value(s): {sorted(invalid)}",
            "hint": "scope must be comma-separated 'title', 'body', or both.",
        })
        return 2
    search_title = "title" in scope_parts
    search_body = "body" in scope_parts

    # Compile pattern.
    flags = 0 if args.case_sensitive else re.IGNORECASE
    try:
        pattern = re.compile(args.pattern, flags)
    except re.error as e:
        c.emit_error({
            "error": f"invalid regex {args.pattern!r}: {e}",
            "hint": "Pattern is a Python regex. Substring search? Use "
                    "the literal characters; meta-chars need escaping.",
        })
        return 2

    limit = max(1, args.limit)
    context = max(0, args.context)

    with open_db(ws_dir / "db.sqlite3") as conn:
        roots = fetch_outline_tree(conn)
    flat = flatten_outline(roots)

    matches: list[dict] = []
    truncated = False

    for sec in flat:
        title_match = None
        if search_title and sec.title and pattern.search(sec.title):
            title_match = sec.title

        body_matches: list[dict] = []
        if search_body and sec.body:
            body_matches = _find_body_matches(sec.body, pattern, context)

        if not title_match and not body_matches:
            continue

        match_entry: dict = {
            "sectionNumber": sec.section_number,
            "title": sec.title,
            "depth": sec.depth,
        }
        if sec.state:
            match_entry["state"] = sec.state
        if title_match is not None:
            match_entry["titleMatch"] = title_match
        if body_matches:
            match_entry["bodyMatches"] = body_matches

        matches.append(match_entry)
        if len(matches) >= limit:
            truncated = True
            break

    response: dict = {
        "filename": filename,
        "pattern": args.pattern,
        "scope": sorted(scope_parts),
        "caseSensitive": bool(args.case_sensitive),
        "matchCount": len(matches),
        "matches": matches,
    }
    if truncated:
        response["truncated"] = True
        response["nextSteps"] = [
            f"Result list capped at {limit}. Narrow the pattern, set "
            f"a tighter scope, or raise --limit if you need more.",
        ]
    c.emit(response, args.pretty)
    return 0


def _find_body_matches(body: str, pattern: re.Pattern, context: int) -> list[dict]:
    """Return one entry per LINE that matches, with surrounding context.

    Each entry: {lineNumber, line, before: [...], after: [...]}.
    lineNumber is 1-based within the body, NOT the source file's line
    number -- bodies in markdown-helper are extracted from between
    headings, and the agent operates per-section.
    """
    lines = body.splitlines()
    out: list[dict] = []
    for idx, line in enumerate(lines):
        if not pattern.search(line):
            continue
        before = lines[max(0, idx - context):idx]
        after = lines[idx + 1:idx + 1 + context]
        out.append({
            "lineNumber": idx + 1,
            "line": line,
            "before": before,
            "after": after,
        })
    return out
