"""Markdown -> sections via mistletoe (CommonMark).

Walks the Document AST looking for top-level Heading/SetextHeading
tokens and their `line_number`. Slices the source text between
consecutive heading lines to extract section bodies VERBATIM (so
fenced code blocks, lists, tables, HTML comments survive round-trip
without re-rendering).

Hard dependency: mistletoe is required, no fallback. The Makefile
installs it.

Two parsing modes:

- `parse_markdown(text) -> list[ParsedSection]`: a flat ordered list
  used by `cmd_open` to populate the workspace cache. Hierarchy is
  reconstructed there from heading level + sibling chain.

- `parse_disk_outline(filename) -> list[DiskSection]`: a fully-computed
  outline (sectionNumber + parent + depth) for a file on disk that
  does NOT have a workspace cache. Used by `markdown_outline` and
  `markdown_read` in their disk-fallback mode. No DB writes; called
  fresh each time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import mistletoe  # noqa: F401
    from mistletoe.block_token import Document, Heading, SetextHeading
    from mistletoe.markdown_renderer import MarkdownRenderer
except ImportError as e:
    raise ImportError(
        "mistletoe is required for markdown parsing. "
        "Install with `pip install mistletoe` (or rerun `make install` "
        "from the markdown-helper source repo)."
    ) from e


@dataclass
class ParsedSection:
    """A single section parsed from source markdown.

    `level` is 1-6 for headed sections (matching the H1-H6 of the
    source). `level` is 0 for the synthetic "preamble" section that
    captures text before the first heading; that section has no title.
    """
    level: int          # 0 = preamble (no heading), 1-6 = H1-H6
    title: str | None   # None for preamble (level 0)
    body: str


def parse_markdown(text: str) -> list[ParsedSection]:
    """Parse markdown text into a flat ordered list of ParsedSections.

    Sections are returned in source order (top to bottom). Hierarchy is
    determined later from the level field by `build_tree`.

    Pre-heading content becomes a level-0 section with title=None and
    is dropped if its body is empty.
    """
    lines = text.splitlines()
    doc = Document(text)

    # Find every top-level Heading/SetextHeading token with its line_number.
    headings: list[tuple[int, int, str]] = []  # (line_number, level, title)
    for child in doc.children or []:
        if isinstance(child, (Heading, SetextHeading)):
            ln = getattr(child, "line_number", None)
            if ln is None:
                continue
            level = getattr(child, "level", 1)
            headings.append((ln, level, _heading_text(child)))

    sections: list[ParsedSection] = []

    # Preamble: text before the first heading line.
    first_heading_line = headings[0][0] if headings else len(lines) + 1
    preamble = "\n".join(lines[: first_heading_line - 1]).strip("\n")
    if preamble.strip():
        sections.append(ParsedSection(level=0, title=None, body=preamble))

    # For each heading, body is the lines from the heading's NEXT line
    # to the next heading's previous line.
    for i, (ln, level, htitle) in enumerate(headings):
        next_ln = headings[i + 1][0] if i + 1 < len(headings) else len(lines) + 1
        body = "\n".join(lines[ln : next_ln - 1]).strip("\n")
        sections.append(ParsedSection(level=level, title=htitle, body=body))

    return sections


def _heading_text(node: Any) -> str:
    """Render a Heading/SetextHeading's content back to markdown source.

    Lifted from mistletoe's own `render_heading` (markdown_renderer.py:266):
    `next(self.span_to_lines(...), "")`. Headings are single-line by
    spec, so taking the first yielded line with an empty default is
    the canonical idiom. Inline markup (backticks, **bold**, *em*,
    [links](...)) is preserved through span_to_lines.
    """
    children = list(getattr(node, "children", []) or [])
    with MarkdownRenderer() as r:
        return next(r.span_to_lines(children, max_line_length=None), "").strip()


# ---------------------------------------------------------------------------
# Outline building -- one shape, two sources
# ---------------------------------------------------------------------------
#
# The same `Section` type represents a section regardless of where it
# came from (mistletoe-parsed file or workspace cache). Workspace-only
# fields (state, seed, session_id) are optional and populated by the
# workspace-mode builder; disk-mode leaves them None.
#
# This is the type that range expansion, rendering, and serialization
# all operate on. There is no separate DiskSection vs workspace section.

@dataclass
class Section:
    """A section in a markdown document outline.

    Carries the agent-visible shape regardless of source (disk parse or
    workspace cache). Workspace-mode populates `state`, `seed`,
    `session_id`; disk-mode leaves them None.
    """
    section_number: str          # "0", "A", "A.1", "A.1.2", ...
    title: str | None            # None only for headless preamble
    depth: int                   # 1 = top-level, 2 = subsection, ...
    body: str
    children: list["Section"] = field(default_factory=list)
    # Workspace-only metadata. None / empty in disk-mode.
    uuid: str | None = None
    state: str | None = None
    seed: str | None = None
    session_id: str | None = None
    tags: list[str] = field(default_factory=list)


def parse_disk_outline(file_path: Path) -> list[Section]:
    """Parse a markdown file from disk into a Section tree.

    Returns top-level sections; each may have children. The forest
    model applies: each top-level titled section becomes its own tree
    (lettered A, B, C). An optional headless preamble at the top is
    addressed as "0".

    State, seed, and session_id are None on every returned Section --
    a file on disk has no per-section workspace metadata.
    """
    text = file_path.read_text(encoding="utf-8")
    flat = parse_markdown(text)
    return _build_section_tree_from_parsed(flat)


def _build_section_tree_from_parsed(flat: list[ParsedSection]) -> list[Section]:
    """Convert a flat list of ParsedSections (with heading levels) into a
    hierarchical Section tree with computed sectionNumbers.

    Forest semantics:
      - level 0 (preamble) -> top-level section "0", title=None
      - level 1 (H1)       -> top-level tree root, lettered A, B, C, ...
      - level 2-6          -> child of the most recent shallower section
    """
    roots: list[Section] = []
    stack: list[tuple[int, Section]] = []
    titled_top_idx = 0  # letter index for top-level titled sections

    for ps in flat:
        if ps.level == 0:
            # Headless preamble: top-level "0".
            sec = Section(
                section_number="0", title=None, depth=1, body=ps.body,
            )
            roots.append(sec)
            stack = []
        elif ps.level == 1 or not stack:
            # Top-level titled section.
            letter = _letter_for_index(titled_top_idx)
            sec = Section(
                section_number=letter, title=ps.title, depth=1, body=ps.body,
            )
            roots.append(sec)
            stack = [(ps.level, sec)]
            titled_top_idx += 1
        else:
            # Descendant: pop until parent has shallower level.
            while stack and stack[-1][0] >= ps.level:
                stack.pop()
            if not stack:
                # Defensive fallback (shouldn't reach here given the
                # earlier branch).
                letter = _letter_for_index(titled_top_idx)
                sec = Section(
                    section_number=letter, title=ps.title, depth=1, body=ps.body,
                )
                roots.append(sec)
                stack = [(ps.level, sec)]
                titled_top_idx += 1
            else:
                _, parent = stack[-1]
                child_idx = len(parent.children) + 1
                sec = Section(
                    section_number=f"{parent.section_number}.{child_idx}",
                    title=ps.title,
                    depth=parent.depth + 1,
                    body=ps.body,
                )
                parent.children.append(sec)
                stack.append((ps.level, sec))

    return roots


def _letter_for_index(idx: int) -> str:
    """0 -> A, 1 -> B, ..., 25 -> Z, 26 -> AA, ..."""
    if idx < 0:
        raise ValueError(f"letter index must be >= 0, got {idx}")
    out = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def flatten_outline(roots: list[Section]) -> list[Section]:
    """Pre-order DFS through a Section tree. Returns sections in render
    order (each section, then its descendants, then the next sibling).
    """
    out: list[Section] = []

    def walk(section: Section) -> None:
        out.append(section)
        for child in section.children:
            walk(child)

    for root in roots:
        walk(root)
    return out


def find_section_by_number(
    roots: list[Section], section_number: str,
) -> Section | None:
    """Find a section by its sectionNumber. Linear scan; OK because
    docs are small."""
    for sec in flatten_outline(roots):
        if sec.section_number == section_number:
            return sec
    return None


# ---------------------------------------------------------------------------
# parse_markdown_to_subtree: take agent-supplied content (which may
# include headings) and produce a (body, children_specs) pair suitable
# for splice_subtree. This is the "auto-split" mechanism behind
# cmd_set { mode: 'subtree' } and cmd_insert { mode: 'subtree' }.
# ---------------------------------------------------------------------------

def parse_markdown_to_subtree(
    content: str, *, target_depth: int,
) -> tuple[str, list[dict]]:
    """Parse `content` and return (body, children_specs).

    body is the prose that appears BEFORE any heading in the content
    (i.e. the target section's own body after the splice). children_specs
    is a recursive list of {title, body, children} dicts that will become
    new descendants of the target.

    Heading levels in the content are normalized RELATIVE to the
    target's depth: the smallest heading level encountered becomes
    target_depth+1 (the target's first-child depth). Subsequent headings
    nest by their RELATIVE level differences from that smallest level.

    Examples (target at depth 2):
      "## Foo\\n\\nx\\n\\n## Bar\\n\\ny"
        -> body="", children=[Foo at depth 3, Bar at depth 3]

      "intro\\n\\n## Foo\\n\\nx"
        -> body="intro", children=[Foo at depth 3]

      "intro only, no headings"
        -> body="intro only, no headings", children=[]

      "# Top\\n\\n## Sub\\n\\nx"  (smallest level = 1)
        -> body="", children=[Top at depth 3, with child Sub at depth 4]

    The target_depth argument exists for documentation but is not
    actually used to set heading levels (depth is computed at render
    time from the tree). It's here to make the relative-nesting
    contract explicit.
    """
    flat = parse_markdown(content)

    # Find body (level 0 prose before any heading).
    body = ""
    headings_only: list[ParsedSection] = []
    for ps in flat:
        if ps.level == 0:
            # Preamble carries the section's own body.
            body = ps.body
        else:
            headings_only.append(ps)

    # Normalize heading levels: shift so the smallest becomes 1
    # (relative depth -- the splice routine doesn't care about absolute
    # heading levels, only the parent/child relationships).
    if not headings_only:
        return body, []
    min_level = min(ps.level for ps in headings_only)
    # Build child specs by walking the flat list with a depth-stack.
    # Each spec is {title, body, children}.
    root_specs: list[dict] = []
    stack: list[tuple[int, dict]] = []  # (relative_level, spec ref)
    for ps in headings_only:
        rel_level = ps.level - min_level + 1  # 1 = root child of target
        spec: dict = {"title": ps.title, "body": ps.body, "children": []}
        # Pop until top of stack is shallower.
        while stack and stack[-1][0] >= rel_level:
            stack.pop()
        if not stack:
            root_specs.append(spec)
        else:
            stack[-1][1]["children"].append(spec)
        stack.append((rel_level, spec))
    return body, root_specs


def has_headings(content: str) -> bool:
    """Quick check: does this content contain any markdown heading?
    Used to decide whether mode:subtree should auto-split."""
    return bool(parse_markdown(content)) and any(
        ps.level > 0 for ps in parse_markdown(content)
    )
