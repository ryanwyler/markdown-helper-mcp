"""markdown_read: read sections from a markdown file.

EXPLICIT SOURCE: --source workspace|disk (no auto -- ambiguity rejected
per design). Mutations remain workspace-only; reads are explicit about
which view they want.

  --source workspace : read from the in-memory workspace (must be
                       open). Returns content with any unsaved edits.
  --source disk      : parse the file fresh from disk. Side effect:
                       writes a touched.json registry entry under
                       .markdown-helper/<dir>/<filename>/ so the file
                       shows up in markdown_list as "touched". A
                       subsequent markdown_open promotes it to a full
                       workspace and removes the touched marker.

OUTPUT FORMAT
=============

  --format structured (default): list of section dicts with body content.
  --format markdown            : render the requested scope as a single
                                 markdown string (the form patch operates
                                 against). Pairs with --scope.

  --scope body    (default)    : just the requested sections' bodies.
  --scope subtree              : the requested sections + ALL their
                                 descendants in render order. Only
                                 meaningful with format=markdown; for
                                 format=structured, range-spec already
                                 handles descendant inclusion.

ASSIST mode (--assist) is preserved -- spawns a sub-agent via the
runner to answer a question about the file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import uuid
from pathlib import Path

from . import _common as c
from ..parse import parse_disk_outline, flatten_outline
from ..ranges import expand_range_spec
from ..sections import fetch_outline_tree
from ..storage import (
    cwd_path,
    file_db_path,
    file_workspace_exists,
    find_helper_root,
    find_project_root,
    open_db,
    resolve_filename,
    write_touched_entry,
)
from ..util import count_words, now


# Where the runner CLI lives. Required for `assist` mode.
RUNNER_CLI = Path.home() / ".runner" / "core" / "runner_core.py"

# Threshold for the "too large" graceful-degradation path.
MAX_RESPONSE_BYTES = 40_000


def register_parser(sub) -> None:
    p = sub.add_parser("read",
                       help="read sections from a markdown file (workspace or disk)")
    p.add_argument("--filename", required=True)
    p.add_argument("--sections",
                   help="optional range spec like 'A.1-A.3,A.5.1' (default: all)")
    p.add_argument("--source", choices=["workspace", "disk"], required=True,
                   help="explicit source. workspace requires file to be open. "
                        "disk parses fresh and writes a touched.json registry entry.")
    p.add_argument("--format", choices=["structured", "markdown"],
                   default="structured",
                   help="structured (default): JSON section list. "
                        "markdown: render as a single markdown string.")
    p.add_argument("--scope", choices=["body", "subtree"], default="body",
                   help="markdown format only. body: requested sections only. "
                        "subtree: requested sections + descendants.")
    p.add_argument("--assist",
                   help="ask a sub-agent a question about the file. "
                        "Spawns opencode via runner; returns a runId for polling.")
    c.add_pretty(p)
    p.set_defaults(func=cmd_read)


def cmd_read(args: argparse.Namespace) -> int:
    if not args.filename:
        c.emit_error({"error": "filename is required"})
        return 2

    project_root = find_project_root()
    try:
        resolved = resolve_filename(args.filename, project_root=project_root)
    except ValueError as e:
        c.emit_error({"error": str(e)})
        return 2
    filename = resolved.canonical
    file_path = resolved.disk_path

    if args.assist:
        # Assist forces a fresh read; doesn't take a source param at
        # the agent layer.
        if not file_path.exists():
            c.emit_error({
                "error": f"file {filename!r} does not exist on disk "
                         f"(looked at {file_path})",
            })
            return 1
        return _cmd_read_assist(args, filename, resolved.workspace_key,
                                file_path, project_root,
                                foreign=resolved.is_foreign)

    return _cmd_read_plain(args, filename, resolved.workspace_key, file_path,
                           foreign=resolved.is_foreign)


def _cmd_read_plain(
    args: argparse.Namespace, filename: str, workspace_key: str,
    file_path: Path, *, foreign: bool,
) -> int:
    helper_root = find_helper_root()

    if args.source == "workspace":
        if not file_workspace_exists(helper_root, workspace_key):
            c.emit_error({
                "error": f"file {filename!r} is not open in the workspace",
                "hint": f"Open it with markdown_open {{ filename: {filename!r} }}, "
                        f"or read from disk with source: 'disk'.",
            })
            return 1
        with open_db(file_db_path(helper_root, workspace_key)) as conn:
            roots = fetch_outline_tree(conn)
        source_label = "workspace"
    else:
        # source == "disk"
        if not file_path.exists():
            c.emit_error({
                "error": f"not found: {filename!r} (looked at {file_path}). "
                         f"Save to write to disk.",
            })
            return 1
        roots = parse_disk_outline(file_path)
        source_label = "disk"
        # Touched.json side effect (skip for foreign files -- their
        # workspace key path under ext/ is fine, but we still write
        # touched markers so foreign files show up in list output).
        try:
            data = file_path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            write_touched_entry(
                helper_root, workspace_key,
                sha=sha, size_bytes=len(data),
                last_read_at=now(),
            )
        except OSError:
            pass

    try:
        selected = expand_range_spec(args.sections or "", roots)
    except ValueError as e:
        c.emit_error({"error": str(e)})
        return 2

    # Markdown format: render the selected scope as a single string.
    if args.format == "markdown":
        rendered = _render_selected_as_markdown(selected, scope=args.scope)
        response: dict = {
            "filename": filename,
            "source": source_label,
            "format": "markdown",
            "scope": args.scope,
            "sectionsRequested": args.sections or None,
            "sectionCount": len(selected),
            "content": rendered,
        }
        if foreign:
            response["readOnly"] = True
        c.emit(response, args.pretty)
        return 0

    # Structured format (default).
    sections_out = [
        {
            "sectionNumber": s.section_number,
            "depth": s.depth,
            "title": s.title,
            "wordCount": count_words(s.body),
            "content": s.body,
        }
        for s in selected
    ]

    response = {
        "filename": filename,
        "source": source_label,
        "format": "structured",
        "sectionsRequested": args.sections or None,
        "sectionCount": len(sections_out),
        "sections": sections_out,
    }
    if foreign:
        response["readOnly"] = True
    if source_label == "workspace":
        response["note"] = (
            "Workspace may have unsaved edits. Use markdown_save to persist."
        )

    # Size-check default reads.
    if not args.sections:
        wire_size = len(json.dumps(response, separators=(",", ":")).encode("utf-8"))
        if wire_size > MAX_RESPONSE_BYTES:
            return _emit_too_large(
                args, filename, source_label, sections_out, wire_size,
            )

    c.emit(response, args.pretty)
    return 0


def _render_selected_as_markdown(selected, *, scope: str) -> str:
    """Render the selected sections as a markdown string. Heading levels
    come from each section's depth."""
    blocks: list[str] = []

    def render_one(sec, *, normalized_depth: int) -> None:
        if sec.title is not None:
            level = min(6, normalized_depth)
            blocks.append("#" * level + " " + sec.title)
        body = (sec.body or "").rstrip()
        if body:
            blocks.append(body)

    if scope == "body":
        for s in selected:
            render_one(s, normalized_depth=s.depth)
    else:
        # Subtree: walk each selected section and its descendants, but
        # present them with depths normalized so the SHALLOWEST in
        # `selected` becomes depth 1. That way the rendered string is
        # self-contained and can be patched cleanly.
        if selected:
            min_depth = min(s.depth for s in selected)
            shift = 1 - min_depth

            def walk(sec) -> None:
                render_one(sec, normalized_depth=sec.depth + shift)
                for child in sec.children:
                    walk(child)

            # Avoid double-rendering descendants if a parent and its child
            # are both in `selected`. The simplest contract: walk only
            # the sections whose parents aren't in the selection.
            ids = {id(s) for s in selected}
            sel_set: set[str] = {s.section_number for s in selected}

            def is_descendant_of_other(sec) -> bool:
                # Check: is any ancestor of sec in sel_set?
                # We don't have parent pointers in `Section`; rely on
                # sectionNumber prefix.
                for other in selected:
                    if other.section_number == sec.section_number:
                        continue
                    if sec.section_number.startswith(other.section_number + "."):
                        return True
                return False

            for s in selected:
                if is_descendant_of_other(s):
                    continue
                walk(s)

    return "\n\n".join(blocks).rstrip() + "\n"


def _emit_too_large(
    args: argparse.Namespace,
    filename: str,
    source: str,
    sections_out: list[dict],
    wire_size: int,
) -> int:
    """Default read would exceed MAX_RESPONSE_BYTES -- return outline
    only with recovery hints."""
    outline = [
        {
            "sectionNumber": s["sectionNumber"],
            "depth": s["depth"],
            "title": s["title"],
            "wordCount": s["wordCount"],
        }
        for s in sections_out
    ]
    preamble_full = next((s for s in sections_out if s["sectionNumber"] == "0"), None)
    intro_full = next((s for s in sections_out if s["sectionNumber"] == "A"), None)

    if intro_full is None or len(intro_full["content"].encode("utf-8")) > MAX_RESPONSE_BYTES:
        too_large = {
            "filename": filename,
            "source": source,
            "tooLargeForMcp": True,
            "estimatedBytes": wire_size,
            "limitBytes": MAX_RESPONSE_BYTES,
            "sectionCount": len(outline),
            "sections": outline,
            "nextSteps": [
                f"Doc is ~{wire_size // 1000}KB. Read specific sections via "
                f"markdown_read sections:'A.1,A.2-A.4', or use assist for a "
                f"sub-agent answer.",
            ],
        }
        c.emit(too_large, args.pretty)
        return 0

    response: dict = {
        "filename": filename,
        "source": source,
        "tooLarge": True,
        "estimatedBytes": wire_size,
        "limitBytes": MAX_RESPONSE_BYTES,
        "sectionCount": len(outline),
        "sections": outline,
    }
    if preamble_full is not None:
        response["preamble"] = {
            "sectionNumber": "0",
            "wordCount": preamble_full["wordCount"],
            "content": preamble_full["content"],
        }
    response["intro"] = {
        "sectionNumber": "A",
        "title": intro_full["title"],
        "depth": intro_full["depth"],
        "wordCount": intro_full["wordCount"],
        "content": intro_full["content"],
    }
    response["nextSteps"] = [
        f"Doc is ~{wire_size // 1000}KB. Read specific sections: "
        f"markdown_read {{ filename: {filename!r}, sections: 'A.1,A.5-A.7' }}.",
        f"Or ask a question: markdown_read {{ filename: {filename!r}, "
        f"assist: '<your question>' }}.",
    ]
    c.emit(response, args.pretty)
    return 0


# ---------------------------------------------------------------------------
# Assist mode (unchanged from v1 but still source-explicit at top)
# ---------------------------------------------------------------------------

def _cmd_read_assist(
    args: argparse.Namespace, filename: str, workspace_key: str,
    file_path: Path, project_root: Path, *, foreign: bool,
) -> int:
    if not RUNNER_CLI.exists():
        c.emit_error({
            "error": "assist requires the runner MCP installed",
            "hint": f"Could not find runner CLI at {RUNNER_CLI}.",
        })
        return 1

    helper_root = find_helper_root()
    if file_workspace_exists(helper_root, workspace_key):
        with open_db(file_db_path(helper_root, workspace_key)) as conn:
            roots = fetch_outline_tree(conn)
    else:
        roots = parse_disk_outline(file_path)

    outline_lines: list[str] = []
    for s in flatten_outline(roots):
        indent = "  " * (s.depth - 1)
        title_text = s.title if s.title is not None else "(preamble)"
        outline_lines.append(f"{indent}- {s.section_number}: {title_text}")
    outline_text = "\n".join(outline_lines)

    file_label = str(file_path) if foreign else filename
    prompt = _build_assist_prompt(file_label, args.assist, outline_text)

    assist_dir = helper_root / "assist"
    assist_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = assist_dir / f"{uuid.uuid4().hex[:12]}.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    sub_cwd = cwd_path(project_root)
    opencode_cmd = (
        f'opencode run --format json --agent build '
        f'--dir {shlex.quote(str(sub_cwd))} '
        f'"$(cat {shlex.quote(str(prompt_path))})"'
    )

    runner_argv = [
        "python3", str(RUNNER_CLI), "start",
        "--cmd", opencode_cmd,
        "--cwd", str(sub_cwd),
        "--name", "md-assist",
        "--description", f"markdown_read assist: {args.assist[:60]}",
        "--no-blocking",
    ]
    try:
        result = subprocess.run(
            runner_argv, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        c.emit_error({
            "error": "runner did not respond within 30 seconds",
            "promptFile": str(prompt_path),
        })
        return 1

    if result.returncode != 0:
        c.emit_error({
            "error": "runner failed to start the assist subprocess",
            "stderr": result.stderr.strip(),
            "promptFile": str(prompt_path),
        })
        return 1

    try:
        runner_response = json.loads(result.stdout)
    except json.JSONDecodeError:
        c.emit_error({
            "error": "could not parse runner response",
            "stdout": result.stdout.strip()[:500],
        })
        return 1

    run_id = runner_response.get("runId")
    if not run_id:
        c.emit_error({"error": "runner response did not include runId"})
        return 1

    response = {
        "filename": filename,
        "assist": {
            "question": args.assist,
            "runId": run_id,
            "promptFile": str(prompt_path),
        },
        "nextSteps": [
            f"Get the response with runner_status {{ runId: {run_id!r}, wait: true }}.",
        ],
    }
    c.emit(response, args.pretty)
    return 0


def _build_assist_prompt(filename: str, question: str, outline_text: str) -> str:
    return f"""You are a read-only sub-agent answering a question about a markdown file.

The file is: {filename}

Read the entire file using your standard file-read tools. Use the
canonical sectionNumbers below in your References and Related reading.

```
{outline_text}
```

Question:

{question}

Format:

## Answer

<your answer, citing specific information>

## References

- <sectionNumber>: <one-line note>

## Related reading

- <sectionNumber> (<title>): <one-line reason>

DO NOT modify the file.
"""
