"""markdown_patch: diff-style edits.

Render a target section (or subtree) as markdown, apply a search-replace
patch, re-parse the result via the auto-split path, splice back into
the workspace. Returns the standard changeReport + userSummary.

PATCH FORMAT
============

Search-replace blocks (Claude/Aider-style):

    <<<<<<< SEARCH
    exact text to find (may span lines)
    =======
    replacement text
    >>>>>>> REPLACE

Multiple blocks per patch are supported -- each is applied in order.
Each block's SEARCH must match exactly ONCE. If it matches zero or
multiple times, the patch is rejected with a structured error pointing
at the offending block.

SCOPE
=====

  --scope body     : the patch applies to the target section's body
                     literally. Headings introduced by the patch
                     remain literal text.

  --scope subtree  : the rendered string includes the target's heading
                     (if any), body, and all descendants in render
                     order. After applying the patch, the result is
                     re-parsed via parse_markdown_to_subtree -- new
                     headings auto-split into descendant sections.
                     The target's existing descendants are replaced.
"""

from __future__ import annotations

import argparse

from . import _common as c
from ..parse import parse_markdown_to_subtree
from ..render import render_document
from ..sections import (
    fetch_children,
    fetch_section,
    section_depth,
    splice_subtree,
    update_section_content,
)
from ..states import STATE_READONLY, auto_transition, needs_force, validate_state
from ..storage import open_db
from ..util import now


# ---------------------------------------------------------------------------
# Search-replace block parsing + application
# ---------------------------------------------------------------------------

SEARCH_HEAD = "<<<<<<< SEARCH"
SEPARATOR = "======="
REPLACE_TAIL = ">>>>>>> REPLACE"


def parse_search_replace_blocks(patch: str) -> list[tuple[str, str]]:
    """Parse a patch string into a list of (search, replace) pairs.

    The format is permissive about surrounding whitespace; the markers
    must each appear on their own line.

    Raises ValueError on malformed blocks.
    """
    lines = patch.splitlines()
    i = 0
    blocks: list[tuple[str, str]] = []
    while i < len(lines):
        line = lines[i].rstrip()
        if line == SEARCH_HEAD:
            # Find separator and tail.
            search_lines: list[str] = []
            replace_lines: list[str] = []
            j = i + 1
            while j < len(lines) and lines[j].rstrip() != SEPARATOR:
                search_lines.append(lines[j])
                j += 1
            if j >= len(lines):
                raise ValueError(
                    f"unterminated SEARCH block starting at line {i + 1} "
                    f"(missing {SEPARATOR!r})"
                )
            j += 1  # past SEPARATOR
            while j < len(lines) and lines[j].rstrip() != REPLACE_TAIL:
                replace_lines.append(lines[j])
                j += 1
            if j >= len(lines):
                raise ValueError(
                    f"unterminated REPLACE block starting at line {i + 1} "
                    f"(missing {REPLACE_TAIL!r})"
                )
            blocks.append(("\n".join(search_lines), "\n".join(replace_lines)))
            i = j + 1
        elif line == "":
            i += 1
        else:
            # Non-empty content outside a block. Allow it -- the patch
            # may have prose around blocks. Skip lines until we hit a
            # marker.
            i += 1
    if not blocks:
        raise ValueError(
            f"patch contains no SEARCH/REPLACE blocks "
            f"(format: {SEARCH_HEAD} ... {SEPARATOR} ... {REPLACE_TAIL})"
        )
    return blocks


def apply_search_replace(text: str, blocks: list[tuple[str, str]]) -> str:
    """Apply each block in order. Each SEARCH must match exactly once
    in the current text (after prior replacements). Raises ValueError
    on zero or multiple matches.
    """
    for idx, (search, replace) in enumerate(blocks):
        count = text.count(search)
        if count == 0:
            raise ValueError(
                f"block {idx + 1}: SEARCH text not found in target. "
                f"Verify the exact bytes (whitespace and line endings count)."
            )
        if count > 1:
            raise ValueError(
                f"block {idx + 1}: SEARCH matched {count} locations. "
                f"Add more surrounding context to make the match unique."
            )
        text = text.replace(search, replace, 1)
    return text


# ---------------------------------------------------------------------------
# Scope rendering: produce the markdown the patch is applied against
# ---------------------------------------------------------------------------

def _render_subtree_for_patch(conn, target_id: str) -> str:
    """Render the target section's subtree as markdown for patching.

    For scope=subtree: includes target's heading + body + descendants
    in render order. Heading levels are recomputed as if the subtree
    were standalone (target at depth 1, children at depth 2, etc.).

    The agent gets back exactly this markdown via markdown_read with
    format=markdown, scope=subtree.
    """
    target = fetch_section(conn, target_id)
    if target is None:
        raise ValueError(f"section {target_id!r} not found")

    blocks: list[str] = []
    # Render target itself.
    if target.title is not None:
        blocks.append("# " + target.title)
    body = (target.content or "").rstrip()
    if body:
        blocks.append(body)
    # Render descendants, normalizing levels so target is depth 1.
    _render_descendants(conn, blocks, parent_id=target_id, depth=2)
    return "\n\n".join(blocks).rstrip() + "\n"


def _render_descendants(conn, blocks: list[str], *, parent_id: str, depth: int) -> None:
    for sec in fetch_children(conn, parent_id):
        if sec.title is not None:
            heading_depth = min(6, depth)
            blocks.append("#" * heading_depth + " " + sec.title)
        body = (sec.content or "").rstrip()
        if body:
            blocks.append(body)
        _render_descendants(conn, blocks, parent_id=sec.id, depth=depth + 1)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def register_parser(sub) -> None:
    p = sub.add_parser("patch",
                       help="apply search-replace edits to a section or subtree")
    p.add_argument("--filename", required=True)
    p.add_argument("--section-number", required=True)
    p.add_argument("--scope", choices=["body", "subtree"], default="body",
                   help="body: patch is applied to the section's body only. "
                        "subtree: patch is applied to the rendered markdown "
                        "of the section + its descendants; result is re-parsed "
                        "via auto-split.")
    p.add_argument("--patch", required=True,
                   help="the patch text containing one or more "
                        "<<<<<<< SEARCH / ======= / >>>>>>> REPLACE blocks")
    p.add_argument("--state",
                   help="post-patch state for the target section (defaults "
                        "to auto-transition based on current state)")
    p.add_argument("--by", help="writer identifier; defaults to 'primary'")
    p.add_argument("--force", action="store_true",
                   help="overwrite a terminal-state section")
    c.add_pretty(p)
    p.set_defaults(func=cmd_patch)


def cmd_patch(args: argparse.Namespace) -> int:
    resolved = c.resolve_workspace_or_die(args, require_writable=True)
    if resolved is None:
        return 1
    _, ws_dir, filename = resolved

    # Parse the patch first -- catch malformed blocks before touching the DB.
    try:
        blocks = parse_search_replace_blocks(args.patch)
    except ValueError as e:
        c.emit_error({"error": str(e)})
        return 2

    by = args.by or "primary"

    with open_db(ws_dir / "db.sqlite3") as conn:
        schema = c.schema_or_die(conn)

        uuid = c.resolve_section_or_die(conn, args.section_number)
        if uuid is None:
            return 1
        sec = fetch_section(conn, uuid)
        if sec is None:
            c.emit_error({"error": f"section {args.section_number!r} not found"})
            return 1

        if sec.state == STATE_READONLY:
            c.emit_error({
                "error": f"section {args.section_number!r} is read-only",
                "readOnly": True,
            })
            return 1

        # Validate explicit state if provided.
        if args.state is not None:
            try:
                validate_state(schema, args.state)
            except ValueError as e:
                c.emit_error({"error": str(e), "validStates": list(schema.names())})
                return 2

        if needs_force(sec.state, schema) and not args.force:
            c.emit_error(c.force_required_error(sec.state, schema))
            return 1

        new_state = (args.state if args.state is not None
                     else auto_transition(sec.state, schema))

        # Compute the source text for the patch.
        if args.scope == "body":
            source_text = sec.content or ""
        else:
            source_text = _render_subtree_for_patch(conn, uuid)

        # Apply the patch.
        try:
            patched_text = apply_search_replace(source_text, blocks)
        except ValueError as e:
            c.emit_error({
                "error": f"patch failed: {e}",
                "scope": args.scope,
                "sectionNumber": args.section_number,
                "hint": (
                    "Read the current scope first: markdown_read "
                    "{ filename, sections: '<sn>', format: 'markdown', "
                    f"scope: {args.scope!r} }} to see the exact text."
                ),
            })
            return 1

        before_snapshot = c.take_outline_snapshot(conn)

        if args.scope == "body":
            update_section_content(
                conn, uuid,
                content=patched_text,
                state=new_state,
                updated_by=by,
                session_id=None,
                now=now(),
            )
            from ..revisions import record_revision
            record_revision(
                conn, section_id=uuid, content=patched_text, state=new_state,
                notes=f"patch ({len(blocks)} block{'s' if len(blocks) != 1 else ''})",
                by=by,
            )
        else:
            # Re-parse the patched text. We rendered the target with
            # its heading at level 1, so the parser sees:
            #   "# <target title>\n\n<body>\n\n## <child>\n\n..."
            # We treat the FIRST heading's body as the target's body
            # and EVERYTHING ELSE as descendants.
            from ..parse import parse_markdown
            flat = parse_markdown(patched_text)
            new_body: str = ""
            new_title: str | None = sec.title  # default: leave unchanged
            child_specs: list[dict] = []

            # Find the level-1 heading (the target itself, by render
            # contract). Bodies for it and its descendants are right
            # there in the parsed output.
            if flat and flat[0].level == 1:
                # Target heading + its body.
                new_title = flat[0].title
                new_body = flat[0].body or ""
                tail = flat[1:]
                # Build the children subtree from the tail.
                stack: list[tuple[int, dict]] = []
                for ps in tail:
                    if ps.level == 0:
                        # Headless preamble in the middle? Fold into
                        # whatever the most recent heading's body is.
                        if stack:
                            stack[-1][1]["body"] = (
                                stack[-1][1].get("body", "") + "\n\n" + (ps.body or "")
                            ).strip()
                        else:
                            new_body = (new_body + "\n\n" + (ps.body or "")).strip()
                        continue
                    spec: dict = {"title": ps.title, "body": ps.body, "children": []}
                    while stack and stack[-1][0] >= ps.level:
                        stack.pop()
                    if not stack:
                        child_specs.append(spec)
                    else:
                        stack[-1][1]["children"].append(spec)
                    stack.append((ps.level, spec))
            else:
                # No level-1 heading at the top -- the patch removed the
                # target's heading. Treat as body-only and use parse_markdown_to_subtree
                # to preserve any nested headings as descendants.
                depth = section_depth(conn, uuid)
                body, child_specs = parse_markdown_to_subtree(
                    patched_text, target_depth=depth,
                )
                new_body = body
                new_title = sec.title  # leave alone

            splice_subtree(
                conn, uuid,
                new_body=new_body if new_body else None,
                new_title=new_title if new_title != sec.title else None,
                children_specs=child_specs,
                state=new_state,
                updated_by=by,
                session_id=None,
                now=now(),
            )
            from ..revisions import record_revision
            record_revision(
                conn, section_id=uuid,
                content=new_body if new_body else "",
                state=new_state,
                notes=f"patch subtree ({len(blocks)} block{'s' if len(blocks) != 1 else ''})",
                by=by,
            )

        after_snapshot = c.take_outline_snapshot(conn)
        report = c.compute_change_report(before_snapshot, after_snapshot, filename)

    response = {
        "filename": filename,
        "sectionNumber": args.section_number,
        "scope": args.scope,
        "blocksApplied": len(blocks),
        "changeReport": report["changeReport"],
        "userSummary": report["userSummary"],
        "outline": report["outline"],
    }
    c.emit(response, args.pretty)
    return 0
