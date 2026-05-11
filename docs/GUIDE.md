# markdown-helper -- Agent Guide

An MCP editor for markdown files. Designed as a document editor; equally usable as an agent's externalized working memory or as the surface for iterative requirements refinement. Agent never writes a whole doc -- each tool call edits one section.

Doc = forest of sections. Top-level lettered (A, B, ...). Children dot-numbered (A.1, A.1.2). Headless preamble = `0`.

This is your one guide. Tool descriptions are self-sufficient; read this for patterns, the state model, anti-patterns, and the three operating modes.

## What this is

```
not_just_a_doc_editor = true (though it is one)
not_a_notebook = true
is = (document editor) + (external working memory) + (requirements refinement surface)
primary_purpose = think and edit faster than you can remember
```

Three operating modes, same tool surface:

1. **Document editing.** Author or refine an `.md` file. The file IS the deliverable. Operator reads it. Prose matters. (Default reference use case.)
2. **Agent brain.** Externalized working memory across long sessions. The file is exhaust; the brain is the point. Operator usually doesn't read it. Cryptic shorthand encouraged.
3. **Requirements refinement.** Iteratively perfect a deep requirements doc with the operator. File IS the deliverable AND working surface. State machine tracks each requirement's review status. Save often.

Pick the mode that matches the work. Use the patterns in section A.6 for the mode you're in.

## Hard rules (memorize, no exceptions)

```
R1. titles are stable handles. section numbers ARE NOT.
    - numbers shift on every insert/delete/move/auto-split
    - persist NOTHING by number (commits, prose, brain entries, messages)
    - resolve title -> number at the moment of the tool call, then forget

R2. state schema is load-bearing. declare it explicitly every open.
    default ["pending","done"] is broken for real work.
    canonical brain schema:
      ["pending","in-progress","blocked","under-review","done","deferred"]
    canonical requirements schema:
      ["draft","in-progress","under-review","accepted","deferred"]

R3. save at natural pauses (brain mode) or after each refinement pass
    (requirements/edit mode), NEVER after every individual `set`.
    - save is a full file write + large response
    - batch 5-20 mutations, then one save

R4. body-text markers + per-section state = redundant durability.
    use both when the work matters. inline `[done]`/`[todo]` in body text
    is content; per-section state enables filterable outlines.

R5. write to brain IMMEDIATELY when you discover, decide, or catch a mistake.
    every time you think "i'll keep this in mind," you are about to fail.
    write the card before the next tool call.
```

## Session open protocol

```python
# step 1: discover what exists
markdown_discover(dir="docs/", filename="<pattern>")
markdown_list()

# step 2: open with explicit schema (pick the one for your mode)
markdown_open(
  filename="docs/PLAN.md",
  states=["pending","in-progress","blocked","under-review","done","deferred"]
)
# tool guarantees: open is non-destructive. reopening preserves state if
# content matches disk, or returns divergence:[...] with NO mutation if
# they diverge. resolve divergence explicitly before writing.

# step 3: filter to live work
markdown_outline(
  filename="docs/PLAN.md",
  source="workspace",
  filterStates="in-progress,blocked,under-review"
)

# step 4: ground in the discipline + decisions
markdown_read(filename, sections="<HOW-TO title>,<FINAL DECISIONS title>",
              source="workspace")

# step 5: read in-progress work bodies
markdown_read(filename, sections="<resolved>", source="workspace")
```

If no `HOW TO USE` section exists yet, you are at session 1. Scaffold it before doing real work (see A.6.11).

## Tool surface

The tools are self-describing in their MCP descriptions; this is the orientation overlay.

### State model

Each section has a state from the workspace's declared **schema**. Schema is set at create/open. Default: `[pending, done]`.

**Declare custom:**

```
# bare strings: positional inference (1st=initial, last=terminal)
states: ["todo", "doing", "done"]

# explicit: object form with role flags
states: [
  {name: "draft"},
  {name: "review", working: true},
  {name: "blocked", needsAttention: true},
  {name: "shipped", terminal: true},
]
```

**Role flags drive tool behavior** (the verbs they reference are described in A.4.2):

- `initial` -- new sections start here; first write to `loaded` lands here.
- `working` -- where `markdown_dispatch` lands a section while a sub-agent works.
- `terminal` -- `force: true` required to overwrite.
- `needsAttention` -- where `markdown_review action: "reject"` lands; shows in `nextFocus`.

**Two pseudo-states** (tool-managed, outside any schema):

- `loaded` -- on every section after a fresh open. First write transitions to schema's `initial`. No force.
- `readonly` -- on every section of a foreign-opened doc. All writes refuse. Promote with `markdown_save asFilename: '<cwd-rel>'`.

**Open is non-destructive.** Reopening a workspace either preserves state (content matches disk) or returns `divergence: [...]` with NO mutation. Resolve by `save` (push), `open reset: true` (discard, adopt disk), or per-section `get` + `set`.

**Save never mutates state.** State survives save + reopen-with-workspace. `close` discards the workspace -- next open falls back to default schema unless you re-pass `states`.

### Tool verbs (when to reach for which)

```
markdown_outline   - SCOREBOARD. read-mostly. filterStates is your friend.
                     use every 5-10 mutations to re-orient.

markdown_read      - BODY READ. specify sections by title-resolved number.
                     format: "markdown" + scope: "subtree" gives you the exact
                     text markdown_patch operates against.

markdown_set       - REPLACE body of one section.
                     mode: "body" (default) treats content as literal text.
                     mode: "subtree" parses headings -> auto-splits into children.
                     force: true required only for terminal-state overwrite.

markdown_insert    - NEW section under/before/after/topLevel.
                     before mode: "subtree", ask: are markdown ## headings
                     inside the content? if yes they auto-split into children.
                     for inline literal headings, use mode: "body".

markdown_patch     - SURGICAL edit inside one section. SEARCH/REPLACE blocks.
                     scope: "body" for in-section edits.
                     scope: "subtree" for edits that may introduce new headings.
                     prefer this for small changes; cheap, audit-friendly.

markdown_review    - STATE TRANSITION with note. NOT for content edits.
                     toState: "<schema-state>" or action: "accept" | "reject".
                     notes field is searchable later. write good notes.

markdown_move      - REPARENT or REORDER. uuid + revision history preserved.
                     under: "<parent-title>" | before: "<sibling>" |
                     after: "<sibling>" | topLevel: true
                     numbers shift on every move; re-check outline between moves.

markdown_delete    - REMOVE. recursive: true by default (deletes descendants).
                     if the content was load-bearing, write a sister card
                     capturing what was deleted and why, BEFORE deleting.

markdown_dispatch  - SPAWN sub-agent on a section. requires a "working" state
                     in your schema for the lifecycle to show in outlines.

markdown_save      - FLUSH workspace to .md file. expensive.
                     batch your edits, save at pauses.

markdown_close     - DROP workspace cache. .md file untouched.
                     refuses on unsaved changes unless force.
```

### Mutation response

Every `set`/`insert`/`delete`/`move`/`patch` returns:

```
{
  filename, sectionNumber?,
  changeReport: {
    inserted: [{sectionNumber, title}],
    deleted:  [{sectionNumber, title}],
    shifted:  [{from, to, title}]    # omitted if >5; replaced with shiftedCount: N
    modified: [{sectionNumber, wordDelta}],
  },
  userSummary: "<filename>:\n  + ...\n  - ...\n  ~ ...\n  * ...",
  outline: [{sectionNumber, depth, title, wordCount, state}, ...]
}
```

- `userSummary` -- surface verbatim to user when they asked for status.
- `outline` -- always present; re-plan against this when sectionNumbers shift.
- `shifted` omitted past 5 -- use `outline` for ground truth.
- `divergence` (on `open`) -- workspace and disk differ; resolve before writing.
- `needsSave` (on `outline`) -- flush at next pause.

When `shiftedCount: N` past 5, the `shifted` array is omitted. Consult `outline` directly. Don't try to back-calculate.

## The three operating modes

Three modes, one tool surface. Pick the mode that matches the work; the patterns in A.6 are tagged for which mode they apply to.

### Mode 1: Document editing

The file IS the deliverable. Operator reads it. Sections evolve toward a final committed shape.

```python
markdown_create(filename="docs/foo.md", sections=[
  {title: "Overview", seed: "..."},
  {title: "Details", seed: "..."},
])
# default schema [pending, done]; sections at pending.

markdown_set(filename, sectionNumber="A", content="...", state="done")
# repeat per section, then:

markdown_save(filename)
# refuses if any section is empty stub (titled leaf, no body, no children).
# pass force: true to save anyway.
```

Overwriting `done` needs `force: true`. Use prose. Optimize for human readability.

### Mode 2: Agent brain

Externalized working memory across long sessions. The `.md` file is exhaust; the brain is the point. Operator usually does not read it; cryptic shorthand is the right speed.

Schema: `["pending","in-progress","blocked","under-review","done","deferred"]`.

Build the canonical brain shape (A.6.6). Write decisions immediately (R5). Re-orient via `markdown_outline filterStates: "in-progress,blocked,under-review"` every 5-10 mutations. Save at natural pauses, not per-edit.

You are writing for future-you, not the operator. That changes the prose: terse, source-cited, one fact per card. Operator may glance occasionally but the brain is yours.

### Mode 3: Requirements refinement

Iteratively perfect a deep requirements doc with the operator. File IS the deliverable AND your working surface. Operator reviews; you draft.

Schema: `["draft","in-progress","under-review","accepted","deferred"]`.

Workflow rhythm:

1. **Draft a requirement** as a topic card. State: `draft`.
2. **Walk the decision matrix** as a sub-section. State: `done` on the matrix walk, `under-review` on the parent.
3. **Operator reviews.** They `markdown_review action: "accept"` (-> `accepted`) or `action: "reject"` (-> `deferred` or back to `draft` with notes).
4. **`markdown_dispatch`** on a complex requirement to a sub-agent for deep elaboration. Set a `working` role on a state in your schema first.
5. **Save more often than brain mode** -- the file is the deliverable, the operator may pull it between turns.
6. **Prose is operator-facing.** Avoid the cryptic shorthand of brain mode. Full sentences. Cite source code via `coder` lookups, paste signatures, quote constants.

Use `markdown_outline filterStates: "draft,under-review"` as the operator-visible scoreboard.

## Canonical patterns

Recipes. The first five (Default doc through Discovery) cover the document-editing surface and work in any mode. The remaining cards (Five canonical shapes, Pseudo-brain cards, Decision matrix walks, Anti-pattern cards, Foreign file workflow, Session-1 scaffold) are brain-mode and requirements-mode disciplines.

### Default doc

(See Mode 1 above for the canonical scaffold. Use this when you just need a doc, not a brain.)

### Todo task tracker

A doc that doubles as a task list.

```python
markdown_create(filename, states=["todo","doing","done"], sections=[
  {title: "Implement search", seed: "acceptance: ..."},
  {title: "Migrate users",    seed: "..."},
])
# tasks come in at "todo" (positional: first state).

markdown_set(filename, sectionNumber="A", state="doing")
markdown_set(filename, sectionNumber="A", state="done", content="<notes>")

markdown_outline(filename, source="workspace", filterStates="todo,doing")
# scoreboard
```

State names are agent's call. `[queued, active, shipped]` works the same.

### Building a large doc

Scaffold + batch-fill + dispatch + patch.

```python
# 1. scaffold
markdown_create(filename, sections=[
  {title: "Overview"}, {title: "Goals"}, {title: "API"}, ...
])

# 2. batch-fill. mode: "subtree" auto-splits headings into descendants.
markdown_set(filename, writes=[
  {sectionNumber: "A", content: "<overview>", state: "done"},
  {sectionNumber: "B", mode: "subtree",
   content: "## Components\n\n...\n\n## Flow\n\n...",
   state: "done"},
])
# response.changeReport shows B.1 + B.2 inserted under B.

# 3. dispatch the deep one
# (declare a working state in your schema first if you want dispatch to
#  land sections in a distinct state; default [pending, done] has none,
#  so the section stays in its current state during dispatch.)
markdown_dispatch(filename, sectionNumber="C", extraInstructions="...")
# poll outline until the section transitions out of working state.

# 4. surgical fix on D
markdown_read(filename, sections="D", source="workspace", format="markdown")
markdown_patch(filename, sectionNumber="D", scope="body",
  patch="<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE")

# 5. save once at the end
markdown_save(filename)
```

### Auto-split

When `markdown_set { mode: "subtree" }` content has markdown headings, headings auto-split into descendant sections.

```python
markdown_set(filename, sectionNumber="B", mode="subtree",
  content="intro\n\n## Foo\n\nx\n\n## Bar\n\ny")

# response:
# changeReport: { inserted: [{B.1, "Foo"}, {B.2, "Bar"}], modified: [{B, +1 word}] }
# userSummary: "file.md:\n  + B.1 \"Foo\"\n  + B.2 \"Bar\"\n  * B  (+1 word)"
```

`markdown_insert` with `mode: "subtree"` does the same -- a new section's content can carry sub-structure that auto-splits into children.

`markdown_patch` with `scope: "subtree"` re-parses after applying the patch -- a patch that introduces a `## Heading` becomes a new descendant section.

SectionNumbers shift on every auto-split. Mutation responses include the post-mutation `outline` for re-planning.

### Discovery before authoring

Find docs without committing to a workspace.

```python
markdown_discover(dir="docs/", filename="/arch/", recursive=true)
# matched: [{filename, sectionCount, firstHeading, workspaceState}]
# workspaceState: "none" | "touched" | "open"
# trailing workspaceMatches: [...] for files in workspace not on disk

markdown_outline(filename="docs/auth.md", source="disk")
# inspects disk; writes touched.json marker. file shows up in
# markdown_list as touched.

markdown_open(filename="docs/auth.md")
# promotes to full workspace; touched.json removed; sections at "loaded".
```

**Gitignore semantics**: discover honors `.gitignore` by default. Naming an ignored directory explicitly opts in for the entire subtree (gitignore inheritance is transitive; respecting it inside an ignored directory would surface nothing).

**Filename matcher**: glob (`*.md`, `REQ_*.md`) OR regex (`/arch/`, `REQ.*` -- auto-detected from metacharacters or `/.../`). Case-insensitive.

### The five canonical shapes (brain mode)

The brain should have these five top-level sections in roughly this order. Titles matter, letters don't.

```
A. HOW TO USE THIS DOCUMENT (read first every session)
   - A.1 Document layout
   - A.2 Session resume protocol (5 sub-cards)
   - A.3 State transitions (5 sub-cards: start, capture, complete, block, defer)
   - A.4 Section references (TITLES not numbers)
   - A.5 Brain shape (topic cards, not prose)
   - A.6 Decision protocol (matrix walk, 6 sub-rules)
   - A.7 Code work uses {coder|equivalent}, never Read/Grep/Glob
   - A.8 No fallbacks
   - A.9 Lazy evaluation discipline
   - A.10 Anti-patterns to avoid (N sub-cards, grow over time)
   - A.11 Foreign markdown editing workflow
   - A.12 Title-as-handle (the rule you keep violating)
   - A.13 Post-compaction self-audit

B. VISION / PRINCIPLES (rarely changes; historical content)

C-Q. Project-specific phases, current-state analysis, target architecture,
     decision logs, open questions, glossary, design analyses.
     Stable historical record. Don't reorganize once written.

S. FINAL DECISIONS (this conversation arc supersedes earlier sections)
   - One card per settled architectural decision.
   - On conflict between any earlier section and S, S wins. Always.

T. ACTIVE EXECUTION TRACKER
   - T.1, T.2, ... one work item per top-level child.
   - Sub-cards for rationale: design Qs, decision matrix walks,
     files touched, acceptance criteria.

U. FOLLOW-UP TASKS (parking lot, state: pending)
   When something cross-cutting surfaces, capture here. Don't derail.
```

### Pseudo-brain cards (the single highest-leverage pattern)

Before any non-trivial refactor:

```python
markdown_insert(under="<phase title>",
                title="Pseudo-brain: current state",
                content="<cryptic shorthand, verified via code-reading tool>",
                state="done")

markdown_insert(under="<phase title>",
                title="Pseudo-brain: target state",
                content="<cryptic shorthand, what I'm building>",
                state="done")
```

Cryptic shorthand example (NOT prose):

```
BridgeRecord: TenantID->SourceTenantID, +TargetTenantID, +SourceTenantName,
+TargetTenantName, Name->BridgeName, BridgeID stays (UUIDv7 server-gen).
FormationConfig: DELETE BridgeID/IsBridged/BridgeAccessMode/BridgeAuthMode
(not rename).
Router.IsBridged: consults bridge service config via config.Resolve, NOT
formation config.
New index: by-source-tenant-id on platform_bridges.
Router.clients: keyed by sourceTenantId.
ConfigResolver: ResolveBridge -> ResolveBridgeBySource(sourceTenantId).
```

The diff between current and target IS the work. Re-read both cards in 60 seconds to recover the entire intent. Operator never reads them. That is correct.

### Decision matrix walks (always record under work item)

When making a real architectural decision:

```python
markdown_insert(
  filename=filename,
  under="<work-item-title>",
  title="Decision matrix walk: <decision question>",
  mode="subtree",
  content="""
## Question
{the specific question being decided}

## Options
(a) {option a, cryptic}
(b) {option b, cryptic}

## Matrix walk
- Rule 1 (numbered design rules): {which rule, which way it tips}
- Rule 2 (existing pattern precedence): {what existing code does}
- Rule 3 (POC/reference evaluation): {what the reference does, or N/A}
- Rule 4 (scope check): {in scope? if not, defer to follow-up}
- Rule 5 (default to simpler): {which is simpler, bounded cost of complex}
- Rule 6 (escalate, don't guess): {if unresolved, what specific question}

## Verdict
{the decision, one sentence}

## Why this is right
{rationale, 3-5 sentences, what would invalidate this}
""",
  state="done"
)
```

`mode: "subtree"` splits this into the parent card plus four named children. Future-you scans the parent, drills to "Matrix walk" sub-card, sees the rule analysis. Operator can audit by pointing at one rule.

### Complex refactor requirement card (current + target + matrix + acceptance)

Use this in Mode 3 (requirements / implementation docs) when documenting a refactor that the operator will review. Distinct from A.6.7 pseudo-brain cards: those are cryptic, private, brain-mode shorthand. This card is operator-facing prose with source citations -- the operator will read it, push back on it, and ultimately accept it.

Structure (the parent card has one-sentence framing in its body; the children carry the substance):

```
markdown_insert under:"<work-item or area title>" title:"<requirement title>"
  content:"<1-2 sentence framing: what this refactor is and why it matters>"
  state:"draft"

# four children under the requirement card, in this order:

markdown_insert under:"<requirement title>" title:"Current state"
  content:"<prose-pseudocode of how the code works today; each load-bearing fact
            cites coder source via {file}:{line} or symbol name; reads top-to-bottom
            so the operator can follow the flow>"
  state:"accepted"

markdown_insert under:"<requirement title>" title:"Target state"
  content:"<prose-pseudocode of how it will work, at the same level of detail as
            Current state so the diff is visually obvious; explicit about what
            DELETES (not renames), what stays put, and what's new>"
  state:"accepted"

markdown_insert under:"<requirement title>" title:"Decision matrix walk"
  mode:"subtree"
  content:<the 6-rule walk per A.6.8, with Question/Options/Matrix walk/Verdict/Why
           this is right as sub-cards>
  state:"accepted"

markdown_insert under:"<requirement title>" title:"Acceptance criteria"
  content:"<bulleted list, each criterion independently testable; include at
            least one 'what would invalidate this' red-team test>"
  state:"accepted"

# optional fifth child, if there are deferred questions:
markdown_insert under:"<requirement title>" title:"Open questions"
  content:"<bulleted list of things the operator needs to resolve before this
            moves from under-review to accepted>"
  state:"draft"
```

Why this structure:

- **Current + Target as peer children** makes the diff visually obvious. The operator scans both children side-by-side and sees the change without reading the matrix walk first. If they want to know why, they drill into the matrix walk; if they want to know it'll work, they drill into acceptance criteria. Each child answers a distinct question.
- **Source citations in Current state** let the operator verify by reading the same code. Without citations, the requirement is speculation. With them, it's a claim that can be checked.
- **Acceptance criteria as a peer of the matrix walk**, not buried inside it. The matrix walk says "this is the right move"; acceptance criteria say "here's how we know it landed correctly." Different questions, different cards.
- **Open questions as a state-bearing card.** Anything blocking final acceptance lives there in `draft` state. The operator can scan `markdown_outline filterStates:"draft"` to see every requirement card with unresolved questions across the doc.

State flow for the parent requirement card:
1. `draft` while you're writing it
2. `under-review` when all four (or five) children are populated and you want operator review
3. `accepted` after operator review passes
4. `deferred` if scope changes or the refactor is postponed

This composes the three primitives -- pseudocode current/target diff (A.6.7's shape, in prose-pseudocode for operator readability), decision matrix walk (A.6.8), and acceptance criteria -- into the canonical shape for any refactor requirement. Don't ship a refactor requirement card missing any of these four; the operator will reject the omissions or, worse, accept them and discover the gap during implementation.

### Anti-pattern cards (write before fixing the mistake)

When you catch yourself doing something wrong:

```python
markdown_insert(
  filename=filename,
  under="Anti-patterns to avoid",
  title="<concise failure mode>",
  content="""
{what happened, one sentence}
{why it was wrong, one sentence}
{what to do instead, one sentence}
{the failure signature: what made me do it, so future-me can catch it}
""",
  state="done"
)
```

Write the card BEFORE fixing the mistake. Future-you reads anti-pattern cards first every session. They grow over time. By session 10 you have 15-20 named anti-patterns specific to your failure modes on this project.

Example anti-pattern that should ALWAYS go in:

```
title: Reference sections by number in commits or messages
body: I referenced E.10.1 in a commit message. Then subtree-insert promoted
      siblings and E.10.1 became E.11. Commit reference now broken.
      Why wrong: numbers shift on every move/insert/delete.
      Do instead: title-as-handle in commits, prose, brain entries.
      Failure signature: I am typing a letter+number, I am probably wrong.
```

### Foreign file workflow (cross-repo editing)

Foreign = file outside your cwd / project root. Foreign-opened files come in `readonly`.

```python
# WRONG: edit it directly via markdown-helper (will open read-only)
# WRONG: edit it directly via shell tools (loses brain integration)

# RIGHT: copy -> edit -> copy back
bash(f"cp {foreign_path} {cwd}/working/docs/")

markdown_open(filename="working/docs/{filename}", states=[...])
# work normally in the workspace

markdown_save(filename="working/docs/{filename}")

bash(f"cp {cwd}/working/docs/{filename} {foreign_path}")
# then commit in foreign repo via shell git
```

Alternative if the tool supports it: `markdown_save asFilename: "<cwd-rel>"` to promote a foreign-opened file into the project. Read the `markdown_save` tool description before relying on it.

The `working/docs/` directory is your scratch space. It is part of your project so the markdown-helper can fully manage it. The original location is untouched until you cp back.

### Session-1 scaffold (no brain exists yet)

```python
markdown_create(
  filename="docs/PROJECT_BRAIN.md",
  states=["pending","in-progress","blocked","under-review","done","deferred"],
  sections=[
    {title: "HOW TO USE THIS DOCUMENT (read first every session)"},
    {title: "FINAL DECISIONS (this conversation arc supersedes earlier sections)"},
    {title: "Active execution tracker"},
    {title: "Follow-up tasks"}
  ]
)

# then immediately populate HOW-TO with the discipline cards from this guide.
# each rule R1-R5 becomes a topic card under HOW-TO. add project-specific
# discipline cards as they emerge (no-fallbacks, lazy-eval, layer rules, etc).
# do this BEFORE any project work. future-you reads HOW-TO every resume.
```

## Write-to-brain triggers

```
write IMMEDIATELY when:
  [ ] a decision is made (commit it before next turn or it is gone)
  [ ] a fact is verified via code-reading (source location + finding)
  [ ] you catch yourself making a mistake (anti-pattern card)
  [ ] a follow-up surfaces (parking lot, don't derail current work)
  [ ] operator gives a directive (sacred operational rule)
  [ ] a matrix walk concludes (verdict + rule analysis)
  [ ] a work item changes state (review with note)
  [ ] you discover a counterintuitive fact about the tooling

write AT NATURAL PAUSES:
  [ ] catch-up sync every 30-60 minutes of work
  [ ] before a long-running build/test command
  [ ] before commit + push
  [ ] before context-shift to another task

do NOT write:
  [ ] "i'll just keep this in mind" -- that is the failure signature, write it
  [ ] chat-only chatter (acks, clarifications, pleasantries)
  [ ] tool output you can re-derive in one call
```

## Post-compaction self-audit

If you are an inheriting instance after memory compression:

```
on session start:
  1. read HOW-TO entirely (slow read, no skim)
  2. read FINAL-DECISIONS entirely
  3. read all work-tracker items currently in-progress
  4. STOP. check before proposing any structural change.

before proposing to reorganize/consolidate/delete brain content:
  ask: "am I about to undo a pattern an earlier card explicitly endorses?"
  if yes -> re-read the endorsing card BEFORE acting.
  compaction softens framing of contested decisions without flipping facts.
  the brain still says what it said. read it before acting.

signals you may have lost shape:
  - calling existing structure "wreckage" or "damage"
  - proposing to merge cards back into prose
  - proposing to delete sub-cards as "redundant"
  - referencing sections by number instead of title
  - feeling rushed to "clean up" before doing real work

if any of these -> pause, re-read the anti-patterns card, re-read the
brain-shape card. then act.
```

## Anti-patterns

Things you will be tempted to do that will hurt you.

- **Don't rewrite the whole doc.** Section-at-a-time.
- **Don't edit a markdown file with `Write`/`Edit` shell tools when you have the helper open.** Shell writes bypass the state machine, skip revision history, and produce a `divergence: [...]` on next open that you then have to resolve. Dogfood the helper. The very act of editing a guide ABOUT the helper with `Write` is the failure signature -- if you catch yourself doing it, stop.
- **Don't write content with headings without `mode: "subtree"`.** Headings stay literal until re-parse.
- **Don't manually diff prose.** Use `markdown_patch` with search-replace.
- **Don't pass huge content via `content` arg if shell mangles quoting.** Use `contentFile`.
- **Don't poll dispatched sections by jobId.** Watch `outline` state transitions.
- **Don't save after every `set`.** Save at pauses.
- **Don't assume sectionNumbers are stable.** They shift on insert/delete/move/auto-split. Use response's `outline`.
- **Don't `mode: "subtree"` on the headless preamble (section 0).** Preamble cannot have logical descendants.
- **Don't reference a section in batch `writes: [...]` that an earlier entry will create.** Batch resolves against pre-batch outline.
- **Don't try to edit a foreign doc directly.** Use the working/docs cp-edit-cp workflow, or promote with `markdown_save asFilename: "<cwd-rel>"`.
- **Don't pass single-state schemas via positional inference.** It is rejected (single state would be both initial and terminal). Use 2+ states or explicit role flags.
- **Don't persist section numbers in commits, prose, or brain entries.** Numbers shift. Titles are stable. Resolve title -> number at the moment of the tool call, then forget.
- **Don't think "i'll keep this in mind."** That is the failure signature. Write the card.

## What this tool is not

```
not a markdown renderer        - don't optimize for human readability in brain mode
not a documentation system     - in brain mode, exhaust is markdown; intent is brain
not transactional across files - each file's workspace is independent
not a replacement for git      - commit the .md file to capture brain history
not free                       - save and large reads cost tokens; batch them
not the only persistence       - also commit code, push branches, store creds
                                 elsewhere; the brain is one layer, not the system
```

## One-paragraph summary

> The markdown-helper is a section-at-a-time editor for markdown files; it also functions as externalized working memory and as the surface for iterative requirements refinement. Open every session with an explicit state schema. Reference sections by title, never by number. Write decisions, rationale, and anti-patterns to the brain immediately -- every time you think "i'll keep this in mind," you are about to fail. Use pseudo-brain cards (current+target state in cryptic shorthand) for any non-trivial refactor. Record decision matrix walks as sub-sections under work items in `mode: "subtree"`. Save at natural pauses, not after every edit. On every mutation response, read `changeReport` and `outline` to re-plan against the post-mutation shape. After memory compression, read HOW-TO and FINAL-DECISIONS in full before proposing structural changes -- the brain still says what it said, and compaction softens framing of decisions without flipping facts. Trust the brain instead of trying to remember.

## Meta-rule

```
if reading this guide and a section confuses you:
  -> that's the guide failing, not you. the next agent gets a clarification.

when you finish your session, before exit:
  -> if you learned something this guide doesn't cover, edit this guide.
     (using the helper, not Write.)
  -> add the anti-pattern card to the brain doc too.
  -> future-you inherits both, in different layers.

this guide is a brain-card itself. keep it current.
```
