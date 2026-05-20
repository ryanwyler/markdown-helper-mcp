# markdown-helper -- Agent Guide

An MCP editor for markdown files. The agent edits one section at a
time -- never the whole doc in a single shot.

Same tool surface, three operating modes (Document, Brain,
Requirements). Pick a mode. The mode determines which patterns and
which level of discipline matter. Most agents reach for Document
mode for a one-off README and don't need most of this guide; agents
running a multi-session brain over months need almost all of it.

Tool descriptions are self-sufficient. Read this guide once for the
shape; come back for patterns or anti-patterns when a specific
situation prompts you.

## How sections are addressed

Doc = forest of sections. Top-level sections are lettered (A, B, ...).
Children are dot-numbered relative to their parent (A.1, A.1.2). A
headless preamble (text before the first heading) is section `0`.

**Two facts that drive almost every rule below:**

- **Section numbers shift** every time a section is inserted,
  deleted, moved, or auto-split. Never persist a number anywhere it
  will outlive the current tool call (commits, prose, brain entries,
  cross-references in this doc).
- **Titles are stable.** Every tool that takes a position target
  (`before` / `after` / `under` / `sectionNumber` on get/set/etc)
  accepts a section's TITLE in place of its number. If two sections
  share a title, you get an `ambiguous title` error with candidate
  sectionNumbers and parent context; pass `parentSection: "<ancestor>"`
  to narrow.
- **Cross-references in section bodies auto-rewrite on shift.** Write a cross-reference as `§<id>` (e.g. `§A.1`, `§B.2.3`, `§0` for the preamble). After insert/move/delete/auto-split, the helper rewrites every `§<old>` in every section's body to `§<new>`. Two-pass with sentinels so chained shifts (`§C.1 -> §D.1` while `§D.1 -> §E.1`) don't collide. Response carries `referenceRewrites: {rewriteCount, sectionsTouched}`; userSummary shows `> N §-references auto-updated across M sections`. Treat in-body refs as self-healing.

  Scope:
  - Only `§<id>` rewrites. Bare `A.1` is ignored (might be a version, path, anything).
  - Fenced code blocks (```` ``` ````, `~~~`) left byte-identical -- quoted source.
  - Inline code spans (`` `§A.1` ``) ARE rewritten -- typographically emphasised live ref.
  - Deletions don't fix dangling refs -- the deleted section has no shift target, prose that names it stays as-is.
  - Refs OUTSIDE the doc (commits, sub-agent prompts, brain entries in other files) are still your problem; the rewrite covers section bodies inside this doc only.

## Universal rules

These apply in every operating mode. Brain mode adds a separate
discipline layer (see Mode 2 below).

```
1. Address sections by TITLE in anything persistent. Number form is
   for ephemeral within-this-call use. Position targets accept either.
   Inside a doc's OWN body, use `§<id>` for cross-references -- the
   helper auto-rewrites these on shift (see "How sections are addressed").

2. Declare a state schema at open time unless the default
   ["pending", "done"] is genuinely all you need. Custom schemas
   persist in the trailer so reopening doesn't lose them.

3. Save at natural pauses, not after every `set`. Save is a full
   file write plus trailer serialization. Batch 5-20 mutations per
   save.

4. Use the right addressing axis for the question:
     state    -> filterStates    "what's pending review?"
     tags     -> tagsContain     "what's billing-related?"
     content  -> markdown_search "where does this token appear?"
   state and tags are orthogonal; a section can be `in-progress` AND
   tagged `billing`.

5. NEVER write code line numbers in persistent content (brain
   entries, requirements, commits). Line numbers rot on the next
   commit. Cite the SYMBOL NAME (function, type, const, method) so
   the doc survives churn. Agents with `coder` use the symbol in
   coder GET; agents without it use the symbol as a grep selector.
   File paths are fine; line numbers are not.
```

That's it for universal. The other rules you might have heard about
("write to brain immediately", "use pseudocode not real code", "post-
compaction audit") apply in brain mode and live there.

## Opening a file

```python
# discover what's around (optional; only when you don't know the path)
markdown_discover(dir="docs/", filename="<pattern>")
markdown_list()

# open with whatever schema fits the work
markdown_open(filename="docs/PLAN.md")
# default schema is [pending, done]. fine for simple editing.
# pass `states=[...]` for brain or requirements mode.

# see what's there (default filter excludes `loaded` and terminal
# states -- shows live work only)
markdown_outline(filename="docs/PLAN.md", source="workspace")
# pass filterStates="all" to see everything (including untouched
# sections from disk). format="compact" for big docs.
```

**Tool guarantees:** open is non-destructive. Reopening preserves
state from the workspace if it still exists, or restores it from the
trailer block (per-section state, schema, archived sections, tags,
seeds) if the workspace was dropped between sessions. Divergence
between workspace and disk is surfaced explicitly; resolve before
writing.

Brain mode has a longer open protocol (read HOW-TO and FINAL
DECISIONS, ground in active work) — see Mode 2.

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

**Role flags drive tool behavior** (the verbs they reference are described under "Tool verbs" below):

- `initial` -- new sections start here; first write to `loaded` lands here.
- `working` -- where `markdown_dispatch` lands a section while a sub-agent works.
- `terminal` -- `force: true` required to overwrite.
- `needsAttention` -- where `markdown_review action: "reject"` lands; shows in `nextFocus`.

**Two pseudo-states** (tool-managed, outside any schema):

- `loaded` -- on every section after a fresh open before the trailer's
  state overrides apply. First write transitions to schema's `initial`. No force.
- `readonly` -- on every section of a foreign-opened doc. All writes refuse. Promote with `markdown_save asFilename: '<cwd-rel>'`.

**`archived` is special.** Any state literally named `archived`, OR
any state with the `archived: true` role flag, gets archive
semantics: sections in this state are HIDDEN from the rendered
markdown body. They live in the trailer's `archived` array. On
reopen, they're restored as children of their original parent at
the end of the chain. Use this for historical content you want to
keep around but stop seeing in the rendered doc.

**Open is non-destructive.** Reopening a workspace either preserves state (content matches disk) or returns `divergence: [...]` with NO mutation. Resolve by `save` (push), `open reset: true` (discard, adopt disk), or per-section `get` + `set`.

**Save never mutates state.** State survives save + reopen-with-workspace. `close` discards the workspace -- but the next open recovers everything from the trailer (see "State preservation across sessions" below). Schema is preserved too. The workspace cache is now a cache, not the source of truth.

### Tags

Per-section identifier labels orthogonal to state. State is "where
is this in the pipeline"; tags are "what is this about."

```python
markdown_set(filename, sectionNumber="A.1", content="...",
             tags=["billing","v1"])
markdown_insert(filename, under="...", title="...",
                tags=["auth","security"])
```

Rules:
- Tags are lowercase-normalized + deduplicated (`["Billing", "billing"]`
  collapses to `["billing"]`).
- No whitespace inside a tag. Tags are identifiers, not notes.
- Max 50 chars per tag.
- On `markdown_set` / `markdown_insert`, `tags=[...]` REPLACES the tag
  set; `tags=[]` clears. For tag-only edits (no body rewrite) and for
  add/remove semantics, use `markdown_tag`:

```python
markdown_tag(filename, sectionNumber="A.1", add=["billing","v1"])     # union
markdown_tag(filename, sectionNumber="A.1", remove=["billing"])       # set difference
markdown_tag(filename, sectionNumber="A.1", set=["billing","v1"])     # replace
markdown_tag(filename, writes=[                                       # batch
  {"sectionNumber":"A","add":["spec"]},
  {"sectionNumber":"B","add":["spec"]},
])
```

Filter sections by tag:

```python
markdown_outline(filename, source="workspace",
                 tagsContain="billing")
# matches sections that carry ALL named tags. comma-separated.
# combine with filterStates: "in-progress" + "billing" = active
# work on a specific topic.
```

In compact format, tags appear as `| [tag1,tag2]` suffix on the
section line. In structured format they're a `tags` array on each
section dict.

### Tool verbs (when to reach for which)

```
markdown_outline   - SCOREBOARD. read-mostly. default filter excludes
                     `loaded` + terminal states (=live work only).
                     - filterStates: "all" / "a,b" / "!loaded,!done"
                     - titleContains: "<substring>" (case-insensitive)
                     - tagsContain: "billing,v1" (ALL named tags)
                     - format: "compact" for big docs (10x smaller)
                     use every 5-10 mutations to re-orient.

markdown_search    - REGEX grep over titles and/or bodies. different
                     shape from outline's titleContains: returns body
                     match lines with context. use for "where does
                     this token appear?"
                     pattern: <regex>; scope: "title" | "body" |
                     "title,body" (default); caseSensitive: false
                     by default.

markdown_read      - BODY READ. specify sections by title or number.
                     format: "markdown" + scope: "subtree" gives you the exact
                     text markdown_patch operates against.

markdown_set       - REPLACE body of one section.
                     mode: "body" (default) treats content as literal text.
                     mode: "subtree" parses headings -> auto-splits into
                       children. UUID-PRESERVING merge by title -- existing
                       children whose title appears in the new content keep
                       their UUID + revision history + dispatched session_id.
                       The agent's main lever for refactoring a subtree
                       without burning audit trail. See Auto-split below.
                     tags: replace tag set (lowercase-normalized).
                     force: true required only for terminal-state overwrite.

markdown_insert    - NEW section under/before/after/topLevel.
                     - position targets accept SECTION TITLES, not just
                       numbers. ambiguous title -> error with candidates.
                       narrow with parentSection: "<ancestor-number>".
                     - mode: "subtree" auto-splits inner headings.
                     - tags: lowercase identifiers for the new section
                       (outer section only in subtree mode; auto-split
                       children are untagged).

markdown_patch     - SURGICAL edit inside one section. SEARCH/REPLACE blocks.
                     scope: "body" for in-section edits.
                     scope: "subtree" for edits that may introduce new headings.
                     prefer this for small changes; cheap, audit-friendly.

markdown_review    - STATE TRANSITION with note. NOT for content edits.
                     toState: "<schema-state>" or action: "accept" | "reject".
                     no-op if already in target state with no notes
                     (response says `noOp: true`).
                     notes field is searchable later. write good notes.

markdown_tag       - TAG EDIT, no body rewrite. Pick exactly one operation:
                     set: [...] (replace), add: [...] (union), remove: [...]
                     (set difference). Batch via writes: [{sectionNumber, ...}].
                     no-op if tag set already matches (response says `noOp: true`).
                     never shifts section numbers.

markdown_move      - REPARENT or REORDER. uuid + revision history preserved.
                     under: "<parent-title>" | before: "<sibling>" |
                     after: "<sibling>" | topLevel: true
                     position targets accept titles; same ambiguity rules
                     as markdown_insert.
                     numbers shift on every move; re-check outline between moves.

markdown_delete    - REMOVE. recursive: true by default (deletes descendants).
                     if the content was load-bearing, archive it instead
                     (state="archived") -- it stays available in the
                     trailer without polluting the rendered doc.

markdown_dispatch  - SPAWN sub-agent on a section. requires a "working" state
                     in your schema for the lifecycle to show in outlines.

markdown_save      - FLUSH workspace to .md file. expensive.
                     - writes a trailer block at file end (HTML comment)
                       carrying schema + per-section state/seed/tags +
                       archived subtrees. invisible in rendered viewers.
                     - asFilename: "<cwd-rel>" copies workspace to a new
                       local path; required for foreign-opened files.
                     batch your edits, save at pauses.

markdown_close     - DROP workspace cache. .md file untouched.
                     refuses on unsaved changes unless force. next open
                     restores state from the trailer.
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
    referenceRewrites: {rewriteCount, sectionsTouched}?,  # present only when shifts triggered §-ref rewrites
  },
  userSummary: "<filename>:\n  + ...\n  - ...\n  ~ ...\n  * ...\n  > N §-references auto-updated across M sections"
}
```

- `userSummary` -- surface verbatim to user when they asked for status.
- `changeReport` -- structured delta. shifts past 5 are summarized
  with `shiftedCount: N`; call `markdown_outline` for the post-state
  tree if you need exact addresses.
- `referenceRewrites` -- `{rewriteCount, sectionsTouched}` summary when shifts triggered §-ref rewrites in section bodies. Absent (and the `>` line absent from userSummary) when no rewrites happened. See "How sections are addressed" for the full rewrite contract.
- mutation responses do NOT include the full post-state outline.
  on big docs that blew past tool-output caps; call `markdown_outline`
  explicitly when you need to re-plan against shifted addresses.
- `divergence` (on `open`) -- workspace and disk differ; resolve before writing.
- `needsSave` (on `outline`) -- flush at next pause.
- `trailerOrphans` (on `open`) -- the file's trailer references
  sections that don't match the current visible doc (someone
  hand-edited the .md, or content drifted). The orphan entries
  show what metadata was dropped + why; decide whether to
  reattach by title-search, archive, or accept the loss.

## The three operating modes

Three modes, one tool surface. Pick the mode that matches the work; each mode's section below owns its mode-specific patterns and discipline.

The mode-agnostic recipes live under **Canonical patterns** below those.

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

Schema: `["pending","in-progress","blocked","under-review","done","archived"]`.

You are writing for future-you, not the operator. That changes the prose: terse, source-cited (by symbol name, not line number), one fact per card. Operator may glance occasionally but the brain is yours.

**Brain-mode extra rules.** These are layered on top of the universal rules from the top of this guide.

```
B1. Write to the brain IMMEDIATELY when you discover a fact, make a
    decision, or catch a mistake. Every "I'll keep this in mind"
    instinct is the failure signature -- write the card before the
    next tool call.

B2. Document code with PSEUDOCODE, not real implementation. Real
    code rots; pseudocode captures intent. Cite symbol names so
    future-you can `coder GET` the actual current code when needed.
    A signature or constant quoted verbatim is fine when the exact
    spelling matters; a whole function body is not.

B3. Retire content with `state: "archived"`, not delete. Archived
    sections are hidden from the rendered doc but preserved in the
    trailer. Reopening restores them under their original parent.
    Delete is for content you'd burn if you could.

B4. Re-orient with `markdown_outline` every 5-10 mutations. The
    default filter excludes `loaded` and terminal states, so the
    bare call shows only live work. On a 400-card brain, this is
    your scoreboard.
```

**Brain-mode patterns** (each has its own section below, in this order):
- **The canonical brain shapes** -- top-level cards every brain should have (HOW-TO, FINAL DECISIONS, Active execution tracker, Follow-up tasks).
- **Post-compaction self-audit** -- the on-resume protocol for an inheriting instance.
- **Write-to-brain triggers** -- the explicit checklist of "when to write a card."
- **Session-1 scaffold** -- bootstrapping a brain doc from zero.
- **Anti-pattern cards** -- write the card BEFORE fixing the mistake; future-you reads them first every session.
- **Decision matrix walks** -- structured architectural decisions as sub-sections under work items.
- **Pseudo-brain cards** -- current-state + target-state in cryptic shorthand, the single highest-leverage pattern for refactors.

#### The canonical brain shapes

The brain should have these top-level sections, identified by TITLE
(letters are positional and will shift; never rely on them):

```
- "HOW TO USE THIS DOCUMENT (read first every session)"
   children (one card per discipline):
     "Document layout"
     "Session resume protocol"
     "State transitions"
     "Section references -- titles not numbers"
     "Brain shape -- topic cards, not prose"
     "Decision protocol (matrix walk)"
     "Code work uses coder-equivalent, never Read/Grep/Glob"
     "No fallbacks"
     "Lazy evaluation discipline"
     "Anti-patterns to avoid" (grows over time; one sub-card per pattern)
     "Foreign markdown editing workflow"
     "Title-as-handle (the rule you keep violating)"
     "Post-compaction self-audit"

- "VISION / PRINCIPLES" (rarely changes; historical content)

- project-specific top-level cards: phases, current-state analysis,
   target architecture, decision logs, open questions, glossary,
   design analyses. Stable historical record. Don't reorganize once
   written.

- "FINAL DECISIONS (this conversation arc supersedes earlier sections)"
   One card per settled architectural decision. On conflict between
   any earlier section and this one, this one wins. Always.

- "Active execution tracker"
   One work item per child card, addressed by title. Sub-cards under
   each work item for rationale: design questions, decision matrix
   walks, files touched, acceptance criteria.

- "Follow-up tasks" (parking lot, state: pending)
   When something cross-cutting surfaces, capture here. Don't derail.
```

Order is the agent's choice. The letters get assigned by insertion
order -- the FIRST topLevel-inserted card is `A`, the second `B`, and
so on. The letters are not stable across the doc's life (move/insert/
delete shifts them). The titles are. Whenever you reference a brain
section -- in a tool call, in commit message, in the brain itself --
use the title.

#### Post-compaction self-audit

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

#### Write-to-brain triggers

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

#### Session-1 scaffold (no brain exists yet)

```python
markdown_create(
  filename="docs/PROJECT_BRAIN.md",
  states=["pending","in-progress","blocked","under-review","done","archived"],
  sections=[
    {title: "HOW TO USE THIS DOCUMENT (read first every session)"},
    {title: "FINAL DECISIONS (this conversation arc supersedes earlier sections)"},
    {title: "Active execution tracker"},
    {title: "Follow-up tasks"}
  ]
)

# then immediately populate HOW-TO with discipline cards from this guide.
# each universal rule (titles-as-handles, schema, save cadence, addressing
# axes, no-line-numbers) becomes a topic card under HOW-TO, alongside the
# brain-mode rules (write-immediately, pseudocode-not-code, archive-not-delete,
# re-orient-frequently). add project-specific discipline cards as they emerge
# (no-fallbacks, lazy-eval, layer rules, etc). do this BEFORE any project work
# -- future-you reads HOW-TO every resume.
```

#### Anti-pattern cards (write before fixing the mistake)

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

#### Decision matrix walks (always record under work item)

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

#### Pseudo-brain cards (the single highest-leverage pattern)

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

### Mode 3: Requirements refinement

Iteratively perfect a deep requirements doc with the operator. File IS the deliverable AND your working surface. Operator reviews; you draft.

Schema: `["draft","in-progress","under-review","accepted","deferred"]`.

Workflow rhythm:

1. **Draft a requirement** as a topic card. State: `draft`.
2. **Walk the decision matrix** as a sub-section under the requirement card. State: `done` on the matrix walk, `under-review` on the parent.
3. **Operator reviews.** They `markdown_review action: "accept"` (-> `accepted`) or `action: "reject"` (-> `deferred` or back to `draft` with notes).
4. **`markdown_dispatch`** on a complex requirement to a sub-agent for deep elaboration. Set a `working` role on a state in your schema first.
5. **Save more often than brain mode** -- the file is the deliverable, the operator may pull it between turns.

**Mode 3 has its own discipline layered on the universal rules.**

```
Q1. Prose is OPERATOR-FACING. Avoid brain-mode cryptic shorthand;
    full sentences. The operator will read this.

Q2. Cite source code by SYMBOL NAME so the operator can verify with
    coder GET. Never paste line numbers (rot on next commit). For
    BEHAVIOR, use pseudocode; for an API CONTRACT where the exact
    spelling matters, quote the signature/constant verbatim. Never
    reproduce a whole implementation -- real code rots faster than
    the requirements that describe it.

Q3. Use `markdown_outline filterStates: "draft,under-review"` as the
    operator-visible scoreboard.
```

**Requirements-mode pattern** (its own section below):
- **Complex refactor requirement card** -- the canonical shape (current state / target state / decision matrix / acceptance criteria) for refactor requirements.

The **Decision matrix walks** pattern is shared with brain mode and is documented under Mode 2 above; reach for it the same way here.

#### Complex refactor requirement card (current + target + matrix + acceptance)

Use this in Mode 3 (requirements / implementation docs) when documenting a refactor that the operator will review. Distinct from the "Pseudo-brain cards" pattern: those are cryptic, private, brain-mode shorthand. This card is operator-facing prose with source citations -- the operator will read it, push back on it, and ultimately accept it.

Structure (the parent card has one-sentence framing in its body; the children carry the substance):

```
markdown_insert under:"<work-item or area title>" title:"<requirement title>"
  content:"<1-2 sentence framing: what this refactor is and why it matters>"
  state:"draft"

# four children under the requirement card, in this order:

markdown_insert under:"<requirement title>" title:"Current state"
  content:"<prose-pseudocode of how the code works today; each load-bearing fact
            cites the SYMBOL NAME (function/type/const/method) so it can be
            located via coder GET or a selector regex; NEVER include line
            numbers -- they go stale on the next commit. Reads top-to-bottom
            so the operator can follow the flow>"
  state:"accepted"

markdown_insert under:"<requirement title>" title:"Target state"
  content:"<prose-pseudocode of how it will work, at the same level of detail as
            Current state so the diff is visually obvious; explicit about what
            DELETES (not renames), what stays put, and what's new>"
  state:"accepted"

markdown_insert under:"<requirement title>" title:"Decision matrix walk"
  mode:"subtree"
  content:<the 6-rule walk per "Decision matrix walks" pattern, with Question/Options/Matrix walk/Verdict/Why
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

This composes the three primitives -- pseudocode current/target diff (the "Pseudo-brain cards" shape, in prose-pseudocode for operator readability), decision matrix walk (the "Decision matrix walks" pattern), and acceptance criteria -- into the canonical shape for any refactor requirement. Don't ship a refactor requirement card missing any of these four; the operator will reject the omissions or, worse, accept them and discover the gap during implementation.

## State preservation across sessions

The workspace cache (per-section state, seeds, tags, archived
subtrees, schema) is no longer the only source of truth. When you
`markdown_save`, all of it gets serialized to a trailer block at
the end of the .md file:

```
<previous markdown content>

<!-- markdown-helper:v1
{
  "v": 1,
  "schema": [{name: "pending", initial: true}, ...],
  "sections": {
    "A.1": {"title": "Active workstream",
            "state": "in-progress",
            "seed": "Track Stripe rollout",
            "tags": ["billing", "v1"]}
  },
  "archived": [
    {"parent": "A", "title": "Old approach", "body": "...",
     "state": "archived", "tags": [...], "children": [...]}
  ]
}
-->
```

The trailer is wrapped in an HTML comment, invisible in any rendered
markdown viewer. It only appears when there's something worth
preserving (non-default state, seeds, tags, archived sections, or a
non-default schema). A doc with vanilla content and default schema
saves to plain markdown with no trailer.

**Why this matters for you.** The brain pattern's promise of
"future-you inherits a navigable index" used to be conditional on
the workspace cache surviving -- which it doesn't across session
compaction, fresh-cwd opens, or another agent picking up the doc
weeks later. With the trailer, the .md file IS the workspace. You
can `markdown_close` aggressively; reopening restores everything.

**What's written to the trailer:**

- `sections`: per-section overrides. Only sections with non-default
  state (i.e. not `loaded` and not the schema's initial state),
  non-empty seed, or non-empty tags get a row. Each row includes
  the section's `title` so the open path can sanity-check the
  address still refers to the same content.
- `archived`: each archived top-of-subtree with its parent's
  sectionNumber (in the visible/post-archive doc), title, body,
  state, seed, tags, and recursive children. Archived subtrees are
  NOT rendered in the visible markdown -- they live entirely in
  the trailer.
- `schema`: only when non-default. Round-trips so reopening with a
  fresh workspace doesn't fall back to `[pending, done]`.

**What happens on open:**

1. Parse the visible markdown body (trailer-stripped). Every section
   comes in at `loaded`.
2. If the trailer carries a schema, set it. (Agent's `states` param
   wins if both are present.)
3. Walk `sections`. For each, look up the section by sectionNumber.
   If the title matches the trailer's stored title, apply
   state/seed/tags. If it doesn't (someone hand-edited the .md),
   the entry is dropped to `trailerOrphans` in the response.
4. Walk `archived`. For each, find the parent by sectionNumber and
   append the subtree as the parent's last child. Missing parent ->
   orphan.

**Orphans don't block open.** They're informational. The agent
inspects `trailerOrphans`, decides if the dropped metadata matters
enough to re-create by hand, and moves on.

**Audit trail.** Sections rehydrated from the trailer come in with
`updatedBy: "trailer"` in their section summary. This is the
breadcrumb that tells you the state didn't come from a live edit
this session -- it came from the trailer's round-trip. Useful when
debugging "why is this section in `done` state, did someone just
edit it?" -- if updatedBy is `trailer`, the answer is "no, it
arrived that way from disk."

**Idempotency.** `save -> close -> open -> save` produces a
byte-identical file. Round-tripping doesn't drift.

**The implication for `markdown_close`.** Closing the workspace is
now cheap. The .md file remembers everything. Close at end of
session, end of task, or when you don't need the workspace cache.
The next open restores fully (unless someone hand-edited between).

## Canonical patterns

Mode-agnostic recipes -- patterns that apply whether you're editing a doc, running a brain, or refining requirements.

For mode-specific patterns:
- **Brain-mode patterns** (canonical brain shapes, pseudo-brain cards, decision matrix walks, anti-pattern cards, write-to-brain triggers, post-compaction self-audit, Session-1 scaffold) live under **Mode 2: Agent brain** above.
- **Requirements-mode patterns** (complex refactor requirement card) live under **Mode 3: Requirements refinement** above.

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

**The power feature.** When you pass `mode: "subtree"` (set, insert) or `scope: "subtree"` (patch) and the content has markdown headings, the headings auto-split into descendant sections in one tool call. No second pass, no rewalking the tree, no manual `markdown_insert` per child.

The non-obvious part that makes this load-bearing: **UUID preservation via title-match merge.** When `markdown_set { mode: "subtree" }` re-spans an existing parent, the helper walks the new content's heading structure against the parent's current children, MATCHING BY TITLE. Existing children whose title appears in the new content keep their UUID, revision history, dispatched session_ids, tags, and seed. Only children without a title match get deleted. Only new headings without an existing match get created. You can re-write a parent's whole subtree freely without losing audit trail or in-flight sub-agent state for the descendants you're keeping.

```python
markdown_set(filename, sectionNumber="B", mode="subtree",
  content="intro\n\n## Foo\n\nx\n\n## Bar\n\ny")
# changeReport: { inserted: [{B.1, "Foo"}, {B.2, "Bar"}], modified: [{B}] }
# userSummary:  "file.md:\n  + B.1 \"Foo\"\n  + B.2 \"Bar\"\n  * B  (+1 word)"
```

Three triggers, same parser:

- **`markdown_set { mode: "subtree" }`** -- re-span an existing section's subtree. The prose before the first heading becomes the target's own body; headings auto-split. **UUID-preserving merge by title** -- the agent's main lever for refactoring a subtree without burning revision history.
- **`markdown_insert { mode: "subtree" }`** -- create a new section AND its descendants in one call. The outer section gets the body before any heading; inner headings become its children. Only the outer section's tags apply; auto-split children come in untagged.
- **`markdown_patch { scope: "subtree" }`** -- apply SEARCH/REPLACE blocks, then re-parse the result. A patch that introduces a `## NewHeading` creates a new descendant; a patch that drops a heading deletes the corresponding subtree (with the same title-match safety).

**The mental model:** the content's heading structure IS the new subtree shape. You author the markdown you want to exist, pass it in subtree mode, and the helper figures out the diff. Heading levels are RELATIVE -- the smallest level in your content becomes the target's first-child depth, and nested headings nest accordingly. You don't have to know what depth the target is at; the parser normalizes.

SectionNumbers shift on every auto-split. Mutation responses no longer include the full post-state outline -- call `markdown_outline` after structural changes if you need exact addresses. Use TITLES in the response's `changeReport.inserted` / `modified` entries to plan follow-up tool calls; titles round-trip across the shift.

When NOT to use subtree mode:

- The content has a literal `## Heading` you want as INLINE TEXT in the body (e.g. a code example, a quoted heading). Use `mode: "body"` (the default) -- everything stays as the section's body, no auto-split.
- You only want to update the parent's body and leave children alone. Use plain `markdown_set` without mode. Subtree mode ALWAYS re-spans the children; passing the same content twice deletes any children whose title isn't in the content.

When you DO want subtree mode:

- Refactoring a section that has sub-sections (you'd otherwise need an insert-per-child, or a delete-all-children-then-insert flow that loses history).
- Initial population from a sketch where you've written the markdown in your head and want it materialized as a navigable tree.
- Patching a code-fenced or prose section in a way that introduces structural sub-headings.

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

### Foreign file workflow (cross-repo editing)

Foreign = file outside your cwd / project root. Address by **absolute
realpath**. Foreign-opened files come in `readonly`. Any mutation
(`set`, `insert`, etc.) refuses with a `readOnly` error pointing at
the escape hatch.

```python
# READ-ONLY browsing of a foreign doc:
markdown_open(filename="/home/ryan/src/other-repo/docs/X.md")
# response.filename is the realpath -- use it in every subsequent call.
# symlinks are resolved (/tmp/sym/X.md -> /tmp/real/X.md returns
# the realpath as the canonical filename).

markdown_outline(filename="/home/ryan/src/other-repo/docs/X.md",
                 source="workspace")
markdown_read(filename="/home/ryan/src/other-repo/docs/X.md",
              sections="A.1", source="workspace")
# read tools work normally.

# TO EDIT: promote into your project via asFilename.
markdown_save(filename="/home/ryan/src/other-repo/docs/X.md",
              asFilename="working/docs/X.md")
# - writes <cwd>/working/docs/X.md (creating dirs as needed)
# - re-keys the workspace to the local path
# - clears the readonly flag so you can edit normally
# refuses if asFilename already exists on disk; pick a different name
# or markdown_open the existing target.

# now edit in your project:
markdown_set(filename="working/docs/X.md", ...)
markdown_save(filename="working/docs/X.md")

# if you need to commit the result back to the foreign repo:
bash(f"cp working/docs/X.md /home/ryan/src/other-repo/docs/X.md")
# then commit in foreign repo via shell git.
```

Why this works: `markdown_open` accepts both cwd-relative paths and
absolute paths. Absolute paths whose realpath lands outside the
project are treated as foreign and mounted read-only. `markdown_save
asFilename` is the supported promotion path -- no shell cp needed
for the workspace handoff.

## Anti-patterns

Things you will be tempted to do that will hurt you.

- **Don't rewrite the whole doc.** Section-at-a-time.
- **Don't edit a markdown file with `Write`/`Edit` shell tools when you have the helper open.** Shell writes bypass the state machine, skip revision history, drop the trailer's metadata, and produce a `divergence: [...]` on next open. The very act of editing a guide ABOUT the helper with `Write` is the failure signature -- if you catch yourself doing it, stop.
- **Don't strip the trailer block from a saved .md file.** The
  `<!-- markdown-helper:v1 ... -->` block at the end is the per-section
  state, schema, tags, and archived subtrees. Deleting it loses all of
  that on next open. Treat the trailer as part of the file.
- **Don't write content with headings without `mode: "subtree"`.** Headings stay literal until re-parse.
- **Don't manually diff prose.** Use `markdown_patch` with search-replace.
- **Don't pass huge content via `content` arg if shell mangles quoting.** Use `contentFile`.
- **Don't poll dispatched sections by jobId.** Watch `outline` state transitions.
- **Don't save after every `set`.** Save at pauses; save costs a full
  file write plus trailer serialization.
- **Don't assume sectionNumbers are stable.** They shift on insert/delete/move/auto-split. Call `markdown_outline` after structural changes; mutation responses don't carry the post-state outline anymore. (Refs inside the doc body written as `§<id>` self-heal -- see the §-rewrite contract under "How sections are addressed".)
- **Don't address sections by sectionNumber when a title would do.**
  Titles round-trip across mutations; numbers don't. `under: "Active workstream"`
  is safer than `under: "T.4"`.
- **Don't write bare `A.1` in section bodies expecting it to track shifts.** Only `§A.1` self-heals. Bare tokens are ignored by the rewrite (could be a version, a path, anything).
- **Don't expect the §-rewrite to fix refs to DELETED sections.** It handles shifts, not deletions. After a delete, dangling refs are an agent decision (rephrase, link to a replacement, leave a note).
- **Don't put live `§<id>` refs inside fenced code blocks.** Fenced code is treated as quoted source and left untouched on shift. Inline code (single backticks) IS rewritten.
- **Don't `mode: "subtree"` on the headless preamble (section 0).** Preamble cannot have logical descendants.
- **Don't reference a section in batch `writes: [...]` that an earlier entry will create.** Batch resolves against pre-batch outline.
- **Don't `markdown_delete` content you might want later.** Move it to
  the `archived` state instead -- it stays available in the trailer,
  re-attaches under its parent on reopen, but doesn't pollute the
  rendered doc. Delete is for content you'd burn if you could.
- **Don't pass single-state schemas via positional inference.** Rejected (would be both initial and terminal). Use 2+ states or explicit role flags.
- **Don't persist section numbers in commits, prose, or brain entries.** Numbers shift. Titles are stable. Resolve title -> number at the moment of the tool call, then forget.
- **Don't write code line numbers in brain entries or requirements.**
  `auth.go:142` is dead the moment someone adds a comment above line
  142. Cite the **symbol name** instead: `EmitAndLog`,
  `Router.ResolveBridge`, `DEFAULT_TIMEOUT_MS`. Agents with `coder`
  do `coder GET symbol`; agents without it use the symbol as a
  grep/ripgrep selector. File paths are fine (stable across line-
  number drift); line numbers are not.
- **Don't paste real implementation code into brain entries or
  requirements.** Real code rots: signatures change, helpers get
  renamed, imports shift. Write pseudocode that captures the INTENT
  (control flow, which config wins, what's cached) and cite symbol
  names so future-you can `coder GET` the actual code when needed.
  Signatures and constants are fine to quote verbatim when the exact
  spelling matters (e.g. an API contract); whole function bodies are
  not. If you find yourself pasting >5 lines of real code, step back
  and summarize the behavior instead.
- **Don't try to use `ext/...` as a filename input.** That's the
  internal on-disk encoding for foreign workspaces. Agents always
  address files by cwd-relative path (for local) or absolute realpath
  (for foreign). Inputs starting with `ext/` are rejected.
- **Don't pass tags with whitespace, empty strings, or >50 chars.**
  Tags are identifiers. If you want notes, use the section body.
- **Don't try to merge tags by passing the new ones alone.** `tags=[X]`
  REPLACES the tag set. To add, read existing tags, union with [X],
  then write the full union.
- **Don't ignore `trailerOrphans` in the open response.** It means
  the file's saved metadata referenced sections that no longer match
  (hand-edited .md). The dropped data is in `droppedMetadata` -- decide
  whether to reattach by title-search or accept the loss.
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

> The markdown-helper is a section-at-a-time editor for markdown files; it also functions as externalized working memory and as the surface for iterative requirements refinement. Open every session with an explicit state schema; the schema and per-section state persist in a trailer block at the end of the .md file, so you can close the workspace aggressively and the next open restores everything. Reference sections by title, never by number, in anything that outlives the current tool call -- position targets on insert/move accept titles uniformly, with `parentSection` to disambiguate. Inside the doc's OWN body, write cross-references as `§<id>` (e.g. `§A.1`) -- the helper auto-rewrites these across insert/move/delete/auto-split so they self-heal. When documenting code, cite SYMBOL NAMES (function, type, const) so future-you can `coder GET` them on demand -- never paste line numbers (rot on next commit) and never paste whole implementations (write pseudocode that captures intent). Use tags as an orthogonal axis to state (state = pipeline position; tags = topic). Archive content you want to keep around but stop seeing in the rendered doc -- `state: "archived"` hides the section from the visible body, stores it in the trailer, and restores it on reopen. Write decisions, rationale, and anti-patterns to the brain immediately -- every time you think "i'll keep this in mind," you are about to fail. Save at natural pauses, not after every edit; the trailer is written on save. On mutation responses, read `changeReport` and `userSummary` -- the post-state outline is no longer included; call `markdown_outline` when you need it. Trust the brain (in the trailer + workspace) instead of trying to remember.

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
      "name": "blocked"
    },
    {
      "name": "under-review"
    },
    {
      "name": "done"
    },
    {
      "name": "archived",
      "terminal": true
    }
  ],
  "sections": {
    "A.4.3": {
      "state": "done",
      "title": "Tool verbs (when to reach for which)"
    },
    "A.5": {
      "state": "done",
      "title": "The three operating modes"
    },
    "A.5.2": {
      "state": "done",
      "title": "Mode 2: Agent brain"
    },
    "A.5.2.1": {
      "state": "done",
      "title": "The canonical brain shapes"
    },
    "A.5.2.2": {
      "state": "done",
      "title": "Post-compaction self-audit"
    },
    "A.5.2.3": {
      "state": "done",
      "title": "Write-to-brain triggers"
    },
    "A.5.2.4": {
      "state": "done",
      "title": "Session-1 scaffold (no brain exists yet)"
    },
    "A.5.2.5": {
      "state": "done",
      "title": "Anti-pattern cards (write before fixing the mistake)"
    },
    "A.5.2.6": {
      "state": "done",
      "title": "Decision matrix walks (always record under work item)"
    },
    "A.5.2.7": {
      "state": "done",
      "title": "Pseudo-brain cards (the single highest-leverage pattern)"
    },
    "A.5.3.1": {
      "state": "done",
      "title": "Complex refactor requirement card (current + target + matrix + acceptance)"
    },
    "A.6": {
      "state": "done",
      "title": "State preservation across sessions"
    },
    "A.7.3": {
      "state": "done",
      "title": "Auto-split"
    }
  },
  "v": 1
}
-->
