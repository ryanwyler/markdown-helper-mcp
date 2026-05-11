"""markdown-helper -- editor-style section-by-section markdown authoring.

Domain modules (storage, sections, parse, render, dispatch) live here.
Command-line / MCP-tool adapters live in `md.cmd`.

The agent-visible surface speaks `sectionNumber` (e.g. "0", "A", "A.1.1",
"B"). Internally everything is keyed by UUID. The mapping is computed at
every operation by walking the linked-list-per-parent chain.

Forest model: a markdown document is a forest of trees, not a single
tree. Each top-level section with a title is a "tree root" and gets a
letter (A, B, C...). The rare "section 0" is a single titleless top-level
section holding preamble prose before any heading.
"""

__version__ = "0.7.0"
