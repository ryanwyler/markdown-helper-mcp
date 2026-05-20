#!/usr/bin/env python3
"""markdown-helper -- editor-style section-by-section markdown authoring.

Agents write giant markdown files in one shot and get them ~40% wrong, then
spiral through revisions. This tool inverts the failure mode: open or create
a markdown file, edit one section at a time with explicit per-section state
and history, save back to disk when ready.

Editor convention:
  markdown_create -- scaffold a new file (must not exist on disk)
  markdown_open   -- ingest an existing file (must exist on disk)
  markdown_set    -- write a section's content (and optionally rename)
  markdown_review -- accept/reject a section
  markdown_save   -- write the workspace state back to the file on disk
  markdown_close  -- drop the workspace cache
  markdown_insert -- insert a new section at a specific position
  markdown_delete -- remove a section
  markdown_move   -- reparent/reorder a section
  markdown_outline / markdown_get / markdown_list / markdown_dispatch / markdown_guide

Section addressing:
  The agent uses sectionNumber: "0" (preamble, headless), "A", "A.1",
  "A.1.1", "B", "B.1", ... Letters identify trees (top-level titled
  sections); numbers identify positions within a tree. Internally
  everything is keyed by UUID; sectionNumber is computed at every
  operation.

Storage:
  <project>/.markdown-helper/<filename>/db.sqlite3
  Per-file SQLite. The path components of `filename` become real
  subdirectories. No index file; the filename is self-describing.

This file is the CLI entry point. All logic lives in `md/` and `md/cmd/`.
"""

import argparse
import os
import sys

# Make `md` importable when run as a script. The installed copy lives at
# ~/.markdown-helper/core/, with `md/` as a sibling of this file. Adding
# the script's parent dir to sys.path lets us `import md.<...>`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from md.cmd import (
    create,
    open as open_cmd,
    close,
    outline,
    get,
    set as set_cmd,
    review,
    save,
    insert,
    delete,
    move,
    dispatch,
    list as list_cmd,
    guide,
    read,
    patch as patch_cmd,
    discover,
    search as search_cmd,
    tag as tag_cmd,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="md_helper", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for module in (
        create, open_cmd, close, outline, get, set_cmd, review, save,
        insert, delete, move, dispatch, list_cmd, guide, read,
        patch_cmd, discover, search_cmd, tag_cmd,
    ):
        module.register_parser(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
