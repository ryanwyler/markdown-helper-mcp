"""markdown_discover: find markdown files in the project without opening them.

Walks the agent's cwd-relative directory looking for .md files matching
a glob or regex filter. Honors the project's .gitignore by default;
agents can override by NAMING an ignored directory in the dir param
(the act of typing the path is the override).

PARAMS
======

  --dir DIR              : directory to search (cwd-relative, default cwd)
  --filename PATTERN     : optional name filter. Auto-detected:
                             plain "REQ_*.md" -> glob (fnmatch)
                             "/req/" or "REQ.*"-> regex
                           Case-insensitive match, case-correct return.
  --recursive            : recurse into subdirectories. Default false.
  --limit N              : cap result count. Default 50.
  --ignore-gitignore     : do NOT honor .gitignore (rarely needed).

RESPONSE
========

  {
    "dir": <cwd-rel dir searched>,
    "filenamePattern": <pattern or null>,
    "recursive": bool,
    "matched": [
      { filename, sectionCount, firstHeading, sizeBytes, mtime,
        source: "disk", workspaceState: "none" | "touched" | "open" }
    ],
    "workspaceMatches": [
      // workspace files matching the pattern that weren't already
      // listed in `matched` (i.e. not on disk under the search dir,
      // but tracked in the workspace).
      { filename, sectionCount, workspaceState: "open" | "touched" }
    ]
  }

The trailing workspaceMatches block separates "files in the project's
filesystem" from "files in the workspace registry that match the
filter but live elsewhere or have been opened from outside."
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
from pathlib import Path

from . import _common as c
from ..parse import parse_disk_outline
from ..storage import (
    cwd_path,
    file_workspace_exists,
    file_touched_exists,
    find_helper_root,
    find_project_root,
    list_touched_workspaces,
    list_workspaces,
)


# ---------------------------------------------------------------------------
# Filter compilation
# ---------------------------------------------------------------------------

def _compile_filter(pattern: str | None):
    """Return a callable matcher(name: str) -> bool. Case-insensitive.

    Detection rules (mirrors coder's SELECTOR auto-detect):
      - wrapped in /.../ -> regex (substring search)
      - contains regex metacharacters -> regex
      - otherwise -> glob (fnmatch)
    """
    if not pattern:
        return lambda name: True
    if pattern.startswith("/") and pattern.endswith("/") and len(pattern) >= 2:
        rx = re.compile(pattern[1:-1], re.IGNORECASE)
        return lambda name: rx.search(name) is not None
    metachars = set(".^$*+?()[]{}|\\")
    if any(ch in metachars for ch in pattern):
        # Regex form, but in a non-/.../ shell-friendly way.
        rx = re.compile(pattern, re.IGNORECASE)
        return lambda name: rx.search(name) is not None
    # Glob (fnmatch is case-sensitive by default; lower both sides).
    pat = pattern.lower()
    return lambda name: fnmatch.fnmatch(name.lower(), pat)


# ---------------------------------------------------------------------------
# Gitignore probing
# ---------------------------------------------------------------------------

def _is_gitignored(project_root: Path, abs_path: Path) -> bool:
    """Shell out to git check-ignore. Returns True if the path is
    gitignored. False on errors (so we err on the side of showing the
    file).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "check-ignore", "-q", str(abs_path)],
            capture_output=True, timeout=5,
        )
        # check-ignore: exit 0 = ignored, 1 = not ignored, others = error.
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# ---------------------------------------------------------------------------
# Walking
# ---------------------------------------------------------------------------

def _iter_md_files(
    root: Path,
    *,
    recursive: bool,
    project_root: Path,
    honor_gitignore: bool,
):
    """Yield Path objects for .md files under `root`.

    Opt-in semantics: if the agent NAMES a gitignored directory as
    `root`, the gitignore check is OFF for the entire subtree.
    Reason: gitignore inheritance is transitive -- if root is ignored,
    every child path is ignored too -- so respecting gitignore inside
    an ignored root means surfacing nothing recursively. Naming an
    ignored dir is itself the override.

    When root is NOT gitignored, gitignore is respected throughout the
    subtree (so the agent doesn't accidentally walk into a
    node_modules sitting inside a non-ignored target).
    """
    if not root.exists() or not root.is_dir():
        return

    root_is_ignored = honor_gitignore and _is_gitignored(project_root, root)
    enforce_inside = honor_gitignore and not root_is_ignored

    if not recursive:
        for entry in sorted(root.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix.lower() != ".md":
                continue
            yield entry
        return

    # Recursive walk. Use os.walk for performance.
    import os
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored subdirectories in-place when gitignore applies.
        if enforce_inside and Path(dirpath) != root:
            dirnames[:] = [
                d for d in dirnames
                if not _is_gitignored(project_root, Path(dirpath) / d)
            ]
        # Always skip dotfile directories (.git, .markdown-helper).
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            if not name.lower().endswith(".md"):
                continue
            full = Path(dirpath) / name
            if enforce_inside and Path(dirpath) != root:
                if _is_gitignored(project_root, full):
                    continue
            yield full


# ---------------------------------------------------------------------------
# Per-file summary
# ---------------------------------------------------------------------------

def _summarize_file(path: Path, helper_root: Path,
                    cwd_rel: str) -> dict:
    """Build the dict for one matched file."""
    try:
        size_bytes = path.stat().st_size
        mtime = int(path.stat().st_mtime)
    except OSError:
        size_bytes = 0
        mtime = 0

    section_count = 0
    first_heading: str | None = None
    try:
        roots = parse_disk_outline(path)
        from ..parse import flatten_outline
        flat = flatten_outline(roots)
        section_count = len(flat)
        for s in flat:
            if s.title:
                first_heading = s.title
                break
    except Exception:
        # Don't crash discover on a single bad file; surface it as
        # a 0-section entry.
        pass

    if file_workspace_exists(helper_root, cwd_rel):
        ws_state = "open"
    elif file_touched_exists(helper_root, cwd_rel):
        ws_state = "touched"
    else:
        ws_state = "none"

    return {
        "filename": cwd_rel,
        "sectionCount": section_count,
        "firstHeading": first_heading,
        "sizeBytes": size_bytes,
        "mtime": mtime,
        "source": "disk",
        "workspaceState": ws_state,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def register_parser(sub) -> None:
    p = sub.add_parser("discover",
                       help="find markdown files matching a pattern, without opening them")
    p.add_argument("--dir", default="",
                   help="directory to search (cwd-relative, default cwd)")
    p.add_argument("--filename",
                   help="optional name filter (glob like '*.md' or "
                        "regex like '/req/'). Case-insensitive.")
    p.add_argument("--recursive", action="store_true",
                   help="recurse into subdirectories (default: no)")
    p.add_argument("--limit", type=int, default=50,
                   help="maximum results (default 50)")
    p.add_argument("--ignore-gitignore", action="store_true",
                   help="do not honor the project's .gitignore")
    c.add_pretty(p)
    p.set_defaults(func=cmd_discover)


def cmd_discover(args: argparse.Namespace) -> int:
    project_root = find_project_root()
    cwd = cwd_path(project_root).resolve()
    helper_root = find_helper_root()

    # Resolve the search directory (cwd-relative).
    if args.dir:
        # Allow .. so an agent in a subdir can still scope to a sibling.
        search_dir = (cwd / args.dir).resolve()
    else:
        search_dir = cwd

    if not search_dir.exists() or not search_dir.is_dir():
        c.emit_error({
            "error": f"directory not found: {args.dir!r}",
            "hint": "Pass a path relative to your cwd that exists.",
        })
        return 1

    matcher = _compile_filter(args.filename)
    honor_gitignore = not args.ignore_gitignore

    matched: list[dict] = []
    matched_keys: set[str] = set()  # cwd-rel filenames already in matched
    for path in _iter_md_files(
        search_dir,
        recursive=args.recursive,
        project_root=project_root,
        honor_gitignore=honor_gitignore,
    ):
        if not matcher(path.name):
            continue
        # Compute cwd-relative form (the canonical filename).
        try:
            cwd_rel = path.relative_to(cwd).as_posix()
        except ValueError:
            # Path is outside cwd (shouldn't happen given how search_dir
            # is constructed, but defensive). Fall back to the absolute
            # path which marks it as foreign to subsequent calls.
            cwd_rel = str(path)
        entry = _summarize_file(path, helper_root, cwd_rel)
        matched.append(entry)
        matched_keys.add(cwd_rel)
        if len(matched) >= args.limit:
            break

    # Now collect workspace-only matches: open or touched workspaces
    # whose filename matches the pattern but isn't already in matched.
    workspace_matches: list[dict] = []
    seen_ws: set[str] = set()
    for canonical, workspace_key in list_workspaces(helper_root):
        if canonical in matched_keys or canonical in seen_ws:
            continue
        # Match the BASE NAME against the filter.
        base = Path(canonical).name
        if not matcher(base):
            continue
        seen_ws.add(canonical)
        workspace_matches.append({
            "filename": canonical,
            "workspaceState": "open",
        })
    for canonical, workspace_key in list_touched_workspaces(helper_root):
        if canonical in matched_keys or canonical in seen_ws:
            continue
        base = Path(canonical).name
        if not matcher(base):
            continue
        seen_ws.add(canonical)
        workspace_matches.append({
            "filename": canonical,
            "workspaceState": "touched",
        })

    response = {
        "dir": args.dir or "",
        "filenamePattern": args.filename,
        "recursive": bool(args.recursive),
        "honorGitignore": honor_gitignore,
        "matchedCount": len(matched),
        "matched": matched,
        "workspaceMatches": workspace_matches,
        "nextSteps": _next_steps(matched, workspace_matches),
    }
    c.emit(response, args.pretty)
    return 0


def _next_steps(matched: list[dict], workspace_matches: list[dict]) -> list[str]:
    out: list[str] = []
    if matched:
        first = matched[0]["filename"]
        out.append(
            f"Inspect a doc without opening it: markdown_outline "
            f"{{ filename: {first!r}, source: 'disk' }}."
        )
        out.append(
            f"Open for editing: markdown_open {{ filename: {first!r} }}."
        )
    if workspace_matches:
        out.append(
            f"{len(workspace_matches)} workspace-only match(es) shown at the "
            f"bottom (open or touched files not under the search dir)."
        )
    if not matched and not workspace_matches:
        out.append("No matches. Try widening with recursive=true or a "
                   "different filename pattern.")
    return out
