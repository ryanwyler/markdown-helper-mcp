"""Range syntax parser for sectionNumber lists.

Supports:
  "A.1"             -- single section
  "A.1,A.3,B"       -- comma-separated list (set semantics, deduplicated)
  "A.1-A.3"         -- inclusive range. Both endpoints must be siblings
                       (same parent + same depth). All sections from
                       the start to the end are included, plus their
                       descendants (so "A.1-A.3" gives A.1, A.1.1, A.1.2,
                       A.2, A.3, A.3.1, ...).
  "A,B"             -- top-level letter sections + their children
  Whitespace is ignored.

Returns sectionNumbers in document order (no duplicates), preserving
the natural reading flow.

Operates on the unified `Section` type from `parse.py` -- workspace
and disk sources both produce `Section` trees, so this module doesn't
care where the outline came from.
"""

from __future__ import annotations

from .parse import Section, find_section_by_number, flatten_outline


def expand_range_spec(
    spec: str, roots: list[Section]
) -> list[Section]:
    """Expand a range spec against an outline tree.

    Returns the sections (with descendants for ranges) in document
    order. Raises ValueError on malformed input or non-sibling range
    endpoints.

    Empty spec -> all sections in the outline (in document order).
    """
    if not spec or not spec.strip():
        return flatten_outline(roots)

    all_sections = flatten_outline(roots)
    by_number = {s.section_number: s for s in all_sections}
    selected_handles: set[int] = set()  # use id() as a unique handle
    selected_in_order: list[Section] = []

    def add(sec: Section) -> None:
        if id(sec) not in selected_handles:
            selected_handles.add(id(sec))
            selected_in_order.append(sec)

    parts = [p.strip() for p in spec.split(",") if p.strip()]
    for part in parts:
        if "-" in part:
            # Range form: "A.1-A.3"
            endpoints = [e.strip() for e in part.split("-")]
            if len(endpoints) != 2 or not endpoints[0] or not endpoints[1]:
                raise ValueError(f"malformed range {part!r}")
            start, end = endpoints
            for sec in _expand_range(by_number, start, end, all_sections):
                add(sec)
        else:
            # Single section reference (with descendants)
            sec = by_number.get(part)
            if sec is None:
                raise ValueError(
                    f"sectionNumber {part!r} not found in document"
                )
            for descendant in flatten_outline([sec]):
                add(descendant)

    # Re-order by document position
    doc_order = {id(s): i for i, s in enumerate(all_sections)}
    selected_in_order.sort(key=lambda s: doc_order.get(id(s), 0))
    return selected_in_order


def _expand_range(
    by_number: dict[str, Section],
    start: str,
    end: str,
    all_sections: list[Section],
) -> list[Section]:
    """Expand `start-end` to all sections from start through end (inclusive),
    plus their descendants. Endpoints must be siblings (same depth + same
    parent prefix)."""
    start_sec = by_number.get(start)
    end_sec = by_number.get(end)
    if start_sec is None:
        raise ValueError(f"sectionNumber {start!r} not found in document")
    if end_sec is None:
        raise ValueError(f"sectionNumber {end!r} not found in document")

    # Validate sibling relationship: same depth, same parent prefix.
    if start_sec.depth != end_sec.depth:
        raise ValueError(
            f"range endpoints must be siblings at the same depth: "
            f"{start!r} (depth {start_sec.depth}) and {end!r} "
            f"(depth {end_sec.depth})"
        )
    start_parent = _parent_address(start)
    end_parent = _parent_address(end)
    if start_parent != end_parent:
        raise ValueError(
            f"range endpoints must share the same parent: "
            f"{start!r} parent is {start_parent!r}, "
            f"{end!r} parent is {end_parent!r}"
        )

    # Walk document order. State machine:
    #   - "looking" until we hit start_sec
    #   - "collecting" until we either hit a section whose number doesn't
    #     start-with start's parent prefix (we've left the parent subtree)
    #     OR we've passed end_sec AND left its subtree
    #
    # For range "A.2-A.4" against [A, A.1, A.2, A.2.1, A.3, A.3.1, A.4, A.5]:
    #   - look until A.2  (start)
    #   - collect: A.2, A.2.1, A.3, A.3.1, A.4
    #   - stop at A.5 (we've passed end and left its subtree -- it's not
    #     a descendant of end and doesn't share end's parent)
    out: list[Section] = []
    state = "looking"  # "looking" -> "collecting" -> done (loop exit)
    passed_end = False
    end_num = end_sec.section_number

    for sec in all_sections:
        if state == "looking":
            if id(sec) == id(start_sec):
                out.append(sec)
                state = "collecting"
                if id(sec) == id(end_sec):
                    passed_end = True
            continue
        # state == "collecting"
        if not passed_end:
            # We haven't passed end yet -- include sec.
            out.append(sec)
            if id(sec) == id(end_sec):
                passed_end = True
            continue
        # We've passed end. Continue collecting only end's descendants.
        if _is_descendant(sec.section_number, end_num):
            out.append(sec)
            continue
        # Not a descendant of end -- we've left end's subtree. Stop.
        break

    return out


def _parent_address(section_number: str) -> str:
    """Return the parent's sectionNumber for a child like "A.1.2".

    "A.1.2" -> "A.1"
    "A.1"   -> "A"
    "A"     -> ""  (top-level has no parent)
    "0"     -> ""
    """
    if "." not in section_number:
        return ""
    return section_number.rsplit(".", 1)[0]


def _is_descendant(candidate: str, ancestor: str) -> bool:
    """True if `candidate` is a descendant of `ancestor` in the section
    number hierarchy. "A.1.2" is a descendant of "A" and "A.1" but not
    of "B" or "A.2".
    """
    if candidate == ancestor:
        return False
    return candidate.startswith(ancestor + ".")
