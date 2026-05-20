# markdown-helper

> Section-at-a-time markdown editor + agent brain. MCP server. Edit docs,
> refine requirements, externalize your working memory.

An MCP server that turns a markdown file into something more useful than a
markdown file. Each section is independently addressable, carries its own
state in a schema you declare, and accumulates a revision history. Edits are
surgical -- one section at a time, search-replace patches inside a section,
sub-trees that auto-split when you paste markdown into them. The agent never
writes a 2000-line one-shot doc that comes back 40% wrong.

That's the editor half. The other half: when the work outlasts a single
context window, the same primitives become an **externalized brain**. State
schemas turn the doc into a filterable scoreboard of in-flight work. The
revision table records why every transition happened. After memory
compression, future-you reads HOW-TO + FINAL-DECISIONS + in-progress
work-items and resumes mid-stride -- no scrollback archaeology, no "what
was I doing again?" tax.

Three modes, same tool surface:

- **Edit a document.** Plans, designs, READMEs, anything that ships as
  prose. Section-aware editing means you refine pieces without rewriting
  neighbors. The state machine tells you what's drafted vs reviewed vs
  shipped at a glance.
- **Refine requirements to perfection.** Iterative passes with an operator
  reviewing each requirement. State transitions (`draft -> under-review ->
  accepted`) carry audit notes. Sub-agent dispatch deepens individual
  requirements without losing the rest of the doc's structure.
- **Externalize working memory.** A brain doc with HOW-TO discipline cards,
  a FINAL-DECISIONS log, an Active-execution-tracker, and a Follow-up parking
  lot. Write the moment you decide; future-you finds it. Survives compaction,
  survives session boundaries, survives parallel agents.

Tools work with Claude Code, opencode, and anything else that speaks MCP.

## Why this exists

Two problems, one tool.

**Problem 1: Agents are terrible at long docs.** Asked to write a 2000-line
requirements doc, an agent piles everything into one giant generation, gets
some part wrong, then spirals through revisions trying to fix it without
understanding which part was bad. Each rewrite drifts further from the
original intent. The same failure shape as long bash one-liners: too much
state, no checkpoints.

`markdown-helper` addresses this with editor conventions:

1. **Open or create.** Structure first. `markdown_open` ingests an existing
   file; `markdown_create` scaffolds a new one with titles + seeds.
2. **Custom state schemas.** Agents declare their workflow vocabulary at
   create-time. `[pending, done]` for simple docs. `[todo, doing, blocked,
   done]` for task trackers. `[draft, under-review, accepted, deferred]` for
   requirements. `[pending, in-progress, blocked, under-review, done,
   deferred]` for a full agent brain.
3. **Section-level edits, three flavors.** `markdown_set` for whole-section
   rewrites. `markdown_insert` with `mode: subtree` for adding structure
   (auto-splits markdown headings into descendant sections). `markdown_patch`
   for surgical search-replace inside a section.
4. **Multi-section batch writes.** One `markdown_set` can touch many sections
   in one SQLite transaction; all-or-nothing on validation failure.
5. **Sub-agent dispatch.** `markdown_dispatch` hands a section to an
   opencode sub-agent with session capture, so rejected revisions resume the
   same session for context-cheap iteration.
6. **Non-destructive open.** Reopening a file reconciles workspace against
   disk; on divergence, the response carries a structured divergence report
   and the workspace is NOT mutated. The agent resolves explicitly.
7. **Save.** State is workflow metadata stored in workspace SQLite, never
   serialized into the markdown file. The .md stays clean for humans.

**Problem 2: Agents forget across sessions.** The default agent loop is
"hold the plan in attention, hope nothing important falls out." On any task
that spans more than a few turns, something always does. Decisions get
re-litigated. Source-citations get lost. Anti-patterns get rediscovered the
hard way, again.

The same tool that fixes problem 1 fixes problem 2, because the primitives
are the same. Externalize the decisions, the rationale, the work-item
status, the anti-patterns. The brain doc becomes the durable layer; chat
scrollback becomes volatile working memory. After memory compression,
future-you doesn't piece together what's happening from logs -- they read
the brain.

The discipline that makes this work is in
[`docs/GUIDE.md`](docs/GUIDE.md) (also served live by `markdown_guide`).
The mechanism is in this repo.

## Install

Prerequisites: `bash`, `python3`, `node` + `npm`, `jq`, `make`, `pip`.

```bash
git clone <this repo>
cd markdown-helper-mcp
make install
```

What `make install` does (idempotent):

1. Installs the `mistletoe` Python library (CommonMark parser).
2. Builds the TypeScript MCP server (`mcp/`).
3. Copies `core/`, `docs/`, and `mcp/dist/` to `~/.markdown-helper/`.
4. Registers the MCP server with **opencode**
   (`~/.config/opencode/opencode.json` → `.mcp.markdown-helper`) and
   **Claude Code** (`~/.claude.json` → `.mcpServers.markdown-helper`).
5. Adds `.markdown-helper` to `.git/info/exclude` (local-only) the first
   time the helper is invoked inside any git repo.

After install: **restart opencode/Claude Code**.

### Uninstall

```bash
make uninstall
```

Removes `~/.markdown-helper/` and both MCP registrations. Per-project
`<git-root>/.markdown-helper/<filename>/` dirs are NOT touched.

---

## Tools

All tools take `filename`. Section-level operations also take
`sectionNumber` (`A`, `A.1.2`, `B`, `0` for headless preamble).
Letters identify trees, dot-numbers identify positions within a
tree. UUIDs are internal; agents never see them.

**Lifecycle**

| Tool | Purpose |
|---|---|
| `markdown_create` | Scaffold a new file (must NOT exist on disk). Pass `sections` (titles + seeds + nested children) and optional `states` (custom schema). |
| `markdown_open` | Open an existing file. Non-destructive: reopens reconcile workspace vs disk and emit a structured `divergence` report on mismatch without mutating. Pass `reset: true` to discard the workspace and adopt disk. Pass `states` only on a fresh open. |
| `markdown_close` | Drop a file's workspace cache. The .md on disk is NOT touched. Refuses by default if there are unsaved changes or running dispatches; `force: true` overrides. |
| `markdown_save` | Render the workspace to disk. `asFilename` writes to a different path (creating dirs); used to promote a foreign read-only doc to a writable local copy. Refuses on empty stub sections unless `force: true`. |

**Read** (all require explicit `source: 'workspace' \| 'disk'`)

| Tool | Purpose |
|---|---|
| `markdown_outline` | Structure + state + save staleness. `filterStates` DSL: omit to exclude `loaded` + terminal states (default "live work" view); `'all'` for everything; `'a,b,c'` positive list; `'!loaded,!done'` negation. `titleContains` filters by case-insensitive title substring. `tagsContain` filters to sections carrying all listed tags. `format: 'compact'` returns single-line strings (~10x smaller). |
| `markdown_read` | Sections + content. `format: 'structured'` (default) returns JSON; `format: 'markdown'` renders as a single string for patching. `scope: 'subtree'` includes descendants. Range spec like `'A.1,A.2-A.4'`. `assist: '<question>'` spawns a sub-agent that reads the file and answers with citations. |
| `markdown_get` | One section + revision history (workspace only). |
| `markdown_list` | Index of all open + touched files. With `filename`, returns full status of one file. `filterStates` filters at file level. |
| `markdown_discover` | Find docs without opening. `dir`/`filename` (glob OR regex)/`recursive`/`limit`. Honors `.gitignore` by default; explicitly naming an ignored dir overrides. |
| `markdown_search` | Regex grep over titles and/or bodies. `pattern` is a Python regex. `scope` is `'title'`, `'body'`, or `'title,body'` (default). Case-insensitive by default; `caseSensitive: true` to disable. Returns matches in document order with `before`/`after` context lines (default 2). Capped at 200 matches by default; raise `limit` or narrow the pattern. |

**Write**

| Tool | Purpose |
|---|---|
| `markdown_set` | Replace section body (or rename via `title`). Single section OR `writes:[...]` batch in one transaction. Mode `body` (default) is literal; mode `subtree` parses headings into descendant sections (auto-split). State must be in declared schema. `force: true` to overwrite a terminal section. |
| `markdown_insert` | Insert a new section at a position (`before`/`after`/`under`/`topLevel`). Mode `subtree` auto-splits headings in content. |
| `markdown_delete` | Remove a section + descendants (recursive default). `recursive: false` refuses if children exist. `force: true` overrides running-dispatch refusal. |
| `markdown_move` | Reparent or reorder. UUID stays stable; `sectionNumber` shifts. |
| `markdown_patch` | Search-replace edits. Multi-block: `<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE`. Each SEARCH must match exactly once. Scope `body` patches the section body; scope `subtree` re-parses for structural patches that introduce new headings. |

**Workflow**

| Tool | Purpose |
|---|---|
| `markdown_dispatch` | Spawn (or `kill: true`) an opencode sub-agent on a section. Section transitions to schema's `working` state; `resultState` overrides. Reuses the section's prior session for revision loops. `extraInstructions` injects free-form text into the sub-agent's prompt. |
| `markdown_review` | Transition a section to a chosen state. `toState: '<name>'` for explicit; `action: 'accept'` maps to schema's terminal state; `action: 'reject'` maps to needsAttention state (requires `notes`). |
| `markdown_tag` | Edit a section's tags without rewriting the body. Pick exactly one of `set: [...]` (replace), `add: [...]` (union), or `remove: [...]` (set diff). Single-section or `writes:[...]` batch. No content required, no force gate, no section-number shift. No-op writes are detected and skipped so revision history stays meaningful. |
| `markdown_guide` | Returns the agent guide. |

**Standard mutation response**: `set`/`insert`/`delete`/`move`/`patch`
all return the same shape -- `changeReport` (structured
`{inserted, deleted, shifted, modified}`), `userSummary`
(human-readable multi-line string), and the post-mutation `outline`
so the agent can re-plan after sectionNumbers shift without an
extra call.

## Filename addressing

There is **one canonical form per file**. The agent always sees the
same string for the same file in every response, regardless of which
input form they used to open it.

- **Local files** are addressed by **cwd-relative path**. An agent at
  `<git-root>/sub/` typing `"test/TEST.md"` addresses
  `<git-root>/sub/test/TEST.md`. The MCP folds the cwd offset into
  paths internally; the agent never has to know it.
- **Foreign files** (anything outside the project) are addressed by
  **absolute realpath**. The agent opens with
  `"/home/ryan/src/other/docs/X.md"`; every response echoes the same
  string back. Symlinks are resolved, so opening
  `/tmp/sym/X.md → /tmp/real/X.md` returns `/tmp/real/X.md` as the
  canonical filename.

A relative path that resolves outside the project (e.g.
`"../../other/docs/X.md"` from a sub-cwd that escapes the git root)
is treated as foreign and the response returns its realpath. So the
agent never has to think about whether they typed a relative or
absolute path -- the canonical form they get back tells them exactly
how to refer to that file from then on.

Foreign files are mounted **read-only**: mutating tools refuse with a
clear error pointing at the escape hatch. To edit a foreign doc, save
it into the current project with
`markdown_save { filename, asFilename: "<cwd-rel-path>" }`. That
writes the file at `<your-cwd>/<asFilename>` and re-keys the workspace
to that local copy, which is then editable.

## How it works

### Storage layout

The per-project store sits at `<git-root>/.markdown-helper/`. Inside
it, each open file gets its own subtree:

```
<git-root>/.markdown-helper/<dir>/<filename>/db.sqlite3
                            └─┬─┘ └────┬───┘
                              │        └─ for LOCAL files: the cwd-
                              │           relative filename (path
                              │           components become real
                              │           subdirectories)
                              │
                              └─ the agent's cwd offset relative to
                                 the project root (empty when cwd ==
                                 project root). Computed once per
                                 process from cwd at startup.
```

So an agent at `<git-root>/sub-tool/` opening `"test/TEST.md"` lands
at `<git-root>/.markdown-helper/sub-tool/test/TEST.md/db.sqlite3`.

Two agents in different subdirs of the same monorepo have isolated
workspaces (no collisions on `"docs/foo.md"`), and each agent works
on the file it would naturally type.

**Foreign (cross-project) workspaces** are encoded under a reserved
`ext/` segment, where `ext/` stands in for the filesystem root `/`:

```
<git-root>/.markdown-helper/<dir>/ext/home/ryan/src/other/docs/X.md/db.sqlite3
                            └─┬─┘ └────────────────┬───────────────┘
                              │                    └─ the absolute
                              │                       realpath, leading
                              │                       slash consumed
                              │                       by ext/
                              │
                              └─ same cwd offset as for local files
```

The `ext/...` form is purely on-disk encoding. The agent never types
or sees it -- they always use the absolute realpath
(`"/home/ryan/src/other/docs/X.md"`) as the filename.

- No index file. The workspace key IS the lookup -- self-describing.
- `.markdown-helper` auto-added to `.git/info/exclude` on first use,
  always at the git-root level (regardless of agent cwd within it).
- The `ext/` prefix is reserved -- a local filename starting with
  `ext/` is rejected, so foreign and local workspaces can never
  collide in the same `<dir>` subtree.

### Tags

Tags are an **orthogonal axis** to state and content. State tracks
pipeline position (`pending -> in-progress -> done`); tags track
topic, ownership, or any other free-form labeling
(`billing`, `urgent`, `api`, `bug`). A section can carry any number
of tags; tags are lowercase-normalized identifiers (whitespace
inside a tag is rejected).

Three verbs touch tags:

- **`markdown_tag`** is the dedicated tag-only verb (set/add/remove,
  single or batch, no body rewrite).
- **`markdown_set`** and **`markdown_insert`** accept an optional
  `tags: [...]` field that replaces the section's tag set as part of
  the same write.

Tags are queryable via `markdown_outline tagsContain: 'a,b'`
(returns sections that carry **all** listed tags). Combine with
`filterStates` and `titleContains` for surgical scoreboards
(`filterStates: 'in-progress,blocked'` + `tagsContain: 'billing'`).

Tags are stored in the workspace's SQL `meta` and round-tripped
through the trailer on save (see below), so they survive
save+reopen.

### Trailer block (round-trip metadata)

Per-section state, seeds, and tags live in the workspace SQLite,
not in the rendered markdown. To make the .md file
**self-contained** -- so dropping the workspace cache (session
compaction, fresh-cwd open, another agent picks up the doc weeks
later) doesn't lose that metadata -- `markdown_save` appends an
HTML comment block at the very end of the file:

```html
<!-- markdown-helper:v1
{
  "v": 1,
  "sections": {
    "A.1": {"state": "in-progress", "seed": "...", "tags": [...]},
    "B.2": {"state": "blocked"}
  },
  "archived": [
    {"parent": "A", "title": "Old approach", "body": "...",
     "state": "archived", "children": [...]}
  ]
}
-->
```

The comment is invisible in rendered markdown but parseable on
reopen. `markdown_open` detects the trailer, parses it, and
applies the metadata over the freshly-ingested workspace. Sections
are keyed by `sectionNumber` (a snapshot of the structure at save
time -- consistent because save renders the file AND writes the
trailer in one operation).

The trailer is opt-out via author edit (delete the comment and
state/seeds/tags are lost on reopen, but the prose is intact) and
gracefully no-ops on files that never had one. It's a thin,
self-describing audit trail, not a replacement for the workspace
DB (no revision history, no UUIDs).

### State machine

States are agent-declared. The workspace's schema lives in the SQL
`meta` table; sections carry a state name from that schema.

**Default schema** (used when `markdown_create` / `markdown_open`
omits `states`):

```
pending -> done
```

New sections enter at `pending` (the schema's first state, marked
`initial`). `done` is the schema's terminal state -- `force=true`
is required to overwrite a section in any terminal state.

**Custom schemas** are declared as a list of names (positional
inference) or a list of objects with explicit role flags:

```json
["todo", "doing", "done"]
```
```json
[
  {"name": "draft"},
  {"name": "review", "working": true},
  {"name": "blocked", "needsAttention": true},
  {"name": "shipped", "terminal": true}
]
```

Role flags drive tool behavior:

- `initial` -- where new sections start; loaded sections transition
  here on first write.
- `working` -- where `markdown_dispatch` lands a section while a
  sub-agent works.
- `terminal` -- force required to overwrite.
- `needsAttention` -- where `markdown_review action: 'reject'`
  lands a section.

**Two pseudo-states** sit outside any declared schema:

- `loaded` -- applied to every section ingested from disk on a
  fresh open. First write transitions to the schema's initial
  state. No force needed.
- `readonly` -- applied to every section of a foreign-opened doc.
  All writes refuse with a structured error pointing at
  `markdown_save asFilename`.

### Editing model

Sections are edited along **three orthogonal axes**:

- **Body** (`markdown_set`, `markdown_insert`, `markdown_patch`) -- the prose.
- **State** (`markdown_review`) -- the pipeline position.
- **Tags** (`markdown_tag`) -- topic / ownership labels.

Each verb touches one axis without disturbing the others (though
`set`/`insert` accept optional `state` and `tags` fields to bundle
axes in a single write).

Three body-edit primitives, ordered by surface area:

1. **`markdown_set`** -- whole-section rewrite. Default mode
   `body` replaces the section's body literally; mode `subtree`
   parses the content via CommonMark, and any markdown headings
   in the content auto-split into descendant sections. `set`
   accepts either a single section or a `writes:[...]` batch
   wrapped in a single SQLite transaction.

2. **`markdown_insert`** -- new section at a position
   (`before`/`after`/`under`/`topLevel`). Same `mode: subtree`
   semantics as `set`: content with internal headings auto-splits
   into the new section's descendants in one call.

3. **`markdown_patch`** -- search-replace edits for surgical
   changes. Format is multi-block: one or more
   `<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE` blocks. Each
   SEARCH must match exactly once; ambiguous matches reject with
   the count and a hint to add context. Scope `body` patches the
   section's body literally; scope `subtree` renders the section
   plus its descendants as markdown, applies the patch, and
   re-parses via auto-split -- so a patch that introduces a new
   heading inside the subtree creates a new descendant section
   automatically.

For markdown parsing, the helper uses
[mistletoe](https://github.com/miyuchina/mistletoe) (CommonMark-
compliant pure-Python). Bodies are extracted verbatim from the
source between heading lines, so fenced code blocks (including
ones with `#` characters that look like headings), lists, tables,
and HTML comments survive round-trip without re-rendering.

### Hierarchy

Sections form a forest -- each top-level heading is a separate
tree. Agent-visible addressing is a letter-and-dot path computed
at every operation:

```
A                       # First top-level section (tree A)
A.1                     ## Subsection of A
A.2                     ## Another subsection of A
A.2.1                   ### Sub-subsection of A.2
B                       # Second top-level section (tree B)
0                       (headless preamble, text before any heading)
```

UUIDs are internal -- the agent never sees them. SectionNumbers
shift on every insert/delete/move (and on auto-split inside `set`/
`insert`/`patch`); the post-mutation `outline` is included in
every mutation response so the agent re-plans without an extra
call. `depth` is 1 for top-level, 2 for first-level descendants,
and so on; the rendered heading level matches `depth`.

Pass nested `children` arrays in the `sections` parameter of
`markdown_create` to scaffold a tree:

```json
[
  {"title": "Auth", "seed": "...", "children": [
    {"title": "API keys"},
    {"title": "OAuth", "children": [{"title": "Code grant"}]}
  ]}
]
```

`markdown_open` rebuilds the same tree from heading depths in the
ingested file (`## A` is depth 1; `### A.1` is its child; `## B` is
A's sibling).

### Save

Pre-order DFS through the tree: each section's heading + body,
then its children, then its next sibling. The doc title becomes
the H1 (depth 1). Heading depth in the rendered output is
reconstructed from the section's `depth`.

**Save never mutates state.** Section states (whatever the agent's
schema calls them) are preserved across `save` -- the only thing
save touches is the rendered markdown on disk and the
`last_saved_at` meta entry.

**Stub gate**: save refuses by default if any section is a titled
leaf with no body and no children (almost certainly an unfilled
scaffold from `markdown_create`). The error names every stub.
Pass `force: true` to save anyway -- the file will have empty
heading lines, which is sometimes intentional.

**Save-as**: `markdown_save asFilename: '<cwd-rel-path>.md'` writes
to a different location, creates intermediate directories as
needed, and re-keys the workspace to the new path. Used to
promote a foreign (cross-project, read-only) doc into the current
project as a writable local copy.

## Layout

```
markdown-helper/
  Makefile
  README.md
  core/
    md_helper_core.py     # thin dispatcher (argparse + cmd module imports)
    md/
      __init__.py
      storage.py          # schema, db open, helper-root discovery,
                          # touched.json registry, schema persistence
      ids.py              # UUID generation, sectionNumber computation
      sections.py         # CRUD on linked-list-per-parent, splice_subtree
      parse.py            # mistletoe -> ParsedSection list,
                          # parse_markdown_to_subtree (for auto-split)
      render.py           # tree walk -> markdown text
      dispatch.py         # opencode spawn, schema-aware reconcile, kill
      revisions.py        # revision history table
      states.py           # Schema dataclass; loaded/readonly pseudo-states;
                          # needs_force(); auto_transition()
      trailer.py          # round-trip metadata: HTML-comment trailer
                          # carrying per-section state/seeds/tags + archived
                          # subtrees, so the .md is self-describing
      util.py             # now(), count_words(), normalize_tags()
      cmd/
        _common.py        # outline snapshots, changeReport + userSummary,
                          # schema validation, common response helpers
        create.py         # one file per CLI subcommand:
        open.py           # ... non-destructive open with reconcile + trailer
        close.py
        outline.py        # ... filterStates DSL + titleContains + tagsContain
        read.py           # ... bulk section reads + assist sub-agent
        get.py            # ... explicit source param
        set.py            # ... writes:[] batch + mode + auto-split + tags
        review.py         # ... toState + accept/reject shortcuts
        tag.py            # set/add/remove on the tag axis; no body rewrite
        save.py           # ... save never mutates state; foreign->local flip;
                          # writes the trailer
        insert.py         # ... mode subtree auto-split + tags
        delete.py
        move.py
        dispatch.py       # ... resultState + schema-aware working state
        list.py           # ... filterStates + touched entries
        guide.py
        patch.py          # search-replace patches
        discover.py       # gitignore-aware doc discovery
        search.py         # regex grep over titles/bodies with context lines
  docs/
    GUIDE.md              # served by markdown_guide
  mcp/
    src/index.ts          # MCP server (stdio); shells out to md_helper_core.py.
                          # Wraps every result with title/metadata/output
                          # for opencode panel rendering (Lever A).
    package.json
    tsconfig.json
```

---

## Conventions

- Sections form a forest. Top-level sections are lettered
  (`A`, `B`, `C`, ...) -- each is its own tree. Descendants are
  dot-numbered (`A.1`, `A.1.2`). The headless preamble (text
  before any heading) is `0`.
- `sectionNumber` is computed at every operation; it shifts on
  insert/delete/move and on auto-split. Every structural mutation
  returns the post-mutation `outline` field so the agent can
  re-plan without an extra call.
- The `seed` field is a contract for what the section should
  cover. Reading a rejected section with `markdown_get` shows the
  reviewer's notes in `history[0].notes`.
- **Three orthogonal axes** per section: body (the prose), state
  (pipeline position from your declared schema), and tags (free-
  form topic labels). The verbs `markdown_set` / `markdown_review`
  / `markdown_tag` each operate on one axis without disturbing the
  others.
- All read tools take an explicit `source: 'workspace' | 'disk'`.
  Disk reads write a `touched.json` registry entry under the
  workspace dir, so an agent that read a doc without opening it
  shows up in `markdown_list` as `workspaceState: 'touched'` --
  no SQLite workspace was created.
- **Saved files are self-describing.** Per-section state, seeds,
  and tags round-trip through an HTML-comment trailer at the end
  of the .md, so dropping the workspace cache doesn't lose
  metadata. The trailer is invisible in rendered markdown.
- Every mutation response includes a `userSummary` string
  (multi-line, human-readable) plus the structured `changeReport`.
  Surface `userSummary` to the user when summarizing a tool call.


<!-- markdown-helper:v1
{
  "schema": [
    {
      "initial": true,
      "name": "pending"
    },
    {
      "name": "in-progress"
    },
    {
      "name": "done",
      "terminal": true
    }
  ],
  "sections": {
    "A.3": {
      "state": "done",
      "title": "Tools"
    },
    "A.5.2": {
      "state": "done",
      "title": "Tags"
    },
    "A.5.3": {
      "state": "done",
      "title": "Trailer block (round-trip metadata)"
    },
    "A.5.5": {
      "state": "done",
      "title": "Editing model"
    },
    "A.6": {
      "state": "done",
      "title": "Layout"
    },
    "A.7": {
      "state": "done",
      "title": "Conventions"
    }
  },
  "v": 1
}
-->
