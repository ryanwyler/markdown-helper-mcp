"""markdown_create: scaffold a new markdown file.

The file must NOT already exist on disk. Creates a workspace cache
populated with the (optional) initial section outline and the agent's
declared state schema. The actual file is NOT written to disk until
markdown_save is called.

Sections are passed as a tree:
  [
    { content: "preamble prose" },                 // section 0 (no title)
    { title: "First H1", seed: "...",
      children: [
        { title: "Sub", seed: "..." },
      ]
    },
    { title: "Second H1" },                        // tree B
  ]

States are passed as either a list of names (positional inference for
initial/terminal) or a list of objects with role flags. If omitted,
defaults to ["pending", "done"].
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from . import _common as c
from ..sections import append_child
from ..states import Schema
from ..storage import (
    file_db_path,
    file_workspace_exists,
    find_helper_root,
    open_db,
    meta_set,
    resolve_filename,
    set_schema,
)
from ..util import now


def register_parser(sub) -> None:
    p = sub.add_parser("create",
                       help="scaffold a new markdown file (must not yet exist on disk)")
    p.add_argument("--filename", required=True,
                   help="path of the file to create, e.g. 'docs/README.md'")
    p.add_argument("--section", dest="section_titles", action="append",
                   help="section title (repeatable, flat outline)")
    p.add_argument("--seed", dest="seeds", action="append",
                   help="seed for the corresponding --section (repeatable, parallel)")
    p.add_argument("--outline-json",
                   help='nested JSON: [{title, seed?, content?, children?}, ...]')
    p.add_argument("--outline-json-stdin", action="store_true",
                   help="read --outline-json from stdin (used by the MCP server "
                        "to bypass argv size limits on large outlines)")
    p.add_argument("--states-json",
                   help='JSON for the state schema. Either ["a","b","c"] for '
                        'positional inference (first=initial, last=terminal) '
                        'or [{"name":"a","initial":true}, ...] for explicit '
                        'role flags. Default: ["pending","done"].')
    c.add_pretty(p)
    p.set_defaults(func=cmd_create)


def cmd_create(args: argparse.Namespace) -> int:
    if not args.filename:
        c.emit_error({"error": "filename is required"})
        return 2
    try:
        # allow_foreign=False -- create only makes sense for local files.
        resolved = resolve_filename(args.filename, allow_foreign=False)
    except ValueError as e:
        c.emit_error({"error": str(e)})
        return 2
    filename = resolved.canonical
    workspace_key = resolved.workspace_key

    file_path = resolved.disk_path
    if file_path.exists():
        c.emit_error({
            "error": f"file {filename!r} already exists on disk",
            "hint": "Use markdown_open to edit the existing file.",
        })
        return 1

    root = find_helper_root()
    if file_workspace_exists(root, workspace_key):
        c.emit_error({
            "error": f"file {filename!r} is already open in the workspace",
            "hint": "Use markdown_outline to inspect, or markdown_close to drop it.",
        })
        return 1

    # Resolve sections payload.
    sections_tree: list[dict[str, Any]] = []
    outline_json = args.outline_json
    if args.outline_json_stdin:
        import sys
        outline_json = sys.stdin.read()
    if outline_json:
        try:
            parsed = json.loads(outline_json)
        except json.JSONDecodeError as e:
            c.emit_error({"error": f"invalid --outline-json: {e}"})
            return 2
        if not isinstance(parsed, list):
            c.emit_error({"error": "--outline-json must be a JSON array"})
            return 2
        sections_tree = parsed
    elif args.section_titles:
        seeds = args.seeds or []
        for i, title in enumerate(args.section_titles):
            seed = seeds[i] if i < len(seeds) else None
            sections_tree.append({"title": title, "seed": seed})

    # Resolve schema.
    if args.states_json:
        try:
            states_raw = json.loads(args.states_json)
        except json.JSONDecodeError as e:
            c.emit_error({"error": f"invalid --states-json: {e}"})
            return 2
        try:
            schema = Schema.from_param(states_raw)
        except ValueError as e:
            c.emit_error({"error": str(e)})
            return 2
    else:
        schema = Schema.default()

    db_path = file_db_path(root, workspace_key)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with open_db(db_path) as conn:
        meta_set(conn, "createdAt", str(now()))
        meta_set(conn, "last_saved_at", "0")  # never saved yet
        meta_set(conn, c.META_FOREIGN, "0")  # local, writable
        set_schema(conn, schema)
        try:
            _insert_tree(conn, sections_tree, parent_id=None,
                         initial_state=schema.initial_state())
        except ValueError as e:
            c.emit_error({"error": str(e)})
            return 2

        from ..sections import fetch_all_sections_in_order
        sections_out = [
            c.summarize_section(conn, s) for s in fetch_all_sections_in_order(conn)
        ]

    response = {
        "filename": filename,
        "fileOnDisk": False,
        "states": schema.to_response(),
        "sections": sections_out,
        "nextSteps": [
            f"Fill sections one at a time with markdown_set "
            f"{{ filename: {filename!r}, sectionNumber, content }}.",
            f"Use markdown_outline {{ filename: {filename!r} }} to see what's pending.",
            f"When done, markdown_save {{ filename: {filename!r} }} writes the file to disk.",
        ],
    }
    c.emit(response, args.pretty)
    return 0


def _insert_tree(
    conn, items: list[dict[str, Any]], *,
    parent_id: str | None,
    initial_state: str,
) -> None:
    """Recursively insert a list of section dicts as children of
    parent_id, preserving order."""
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each outline item must be an object")
        title = item.get("title")
        seed = item.get("seed")
        content = item.get("content")
        if title is None and parent_id is not None:
            raise ValueError("only top-level sections may have title=None (preamble)")
        new_section = append_child(
            conn,
            parent_id=parent_id,
            title=title,
            seed=seed,
            content=content,
            state=initial_state,
            updated_by="create",
            now=now(),
        )
        children = item.get("children")
        if children:
            if not isinstance(children, list):
                raise ValueError("'children' must be a JSON array")
            _insert_tree(conn, children, parent_id=new_section.id,
                         initial_state=initial_state)
