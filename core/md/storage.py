"""Storage primitives: project root discovery, helper-root layout, db open.

The agent works on markdown files using their FILENAME. There are two
flavors:

LOCAL files (the common case): filename is a path relative to the
**agent's cwd within the project**, NOT the project root. So a doc
that lives at <project>/markdown-helper/test/TEST.md is addressed as
"test/TEST.md" by an agent whose cwd is <project>/markdown-helper, and
as "markdown-helper/test/TEST.md" by an agent whose cwd is the project
root. Each agent uses the filename it would naturally type.

To make this work, every read/write of a markdown file is rooted at
<project_root>/<dir>/<filename> where `dir` is the agent's cwd offset
relative to the project root (empty when cwd == project root). Workspace
caches are likewise scoped under the dir so two agents in different
subdirs of the same monorepo do not collide on the same workspace key:

    <project>/.markdown-helper/<dir>/<filename>/db.sqlite3
    <project>/<dir>/<filename>                                  (file on disk)

`dir` is computed once per process from cwd at first access and never
exposed to the agent.

FOREIGN files (cross-project, opened read-only): filename is an
absolute path on the host filesystem. The canonical key gets the
"ext/" prefix; the leading slash of the absolute path is consumed.
Foreign workspaces are scoped under the same agent dir so different
cwds within a project see independent foreign caches:

    /home/ryan/foo/docs/X.md
        -> canonical: ext/home/ryan/foo/docs/X.md
        -> workspace: <project>/.markdown-helper/<dir>/ext/home/ryan/foo/docs/X.md/db.sqlite3
        -> disk path: /home/ryan/foo/docs/X.md   (the original)

The "ext/" prefix is reserved -- normalize_filename rejects any local
filename that begins with it. Foreign files are opened in read-only
mode (enforced at the cmd layer); to edit a foreign file the agent
must first save it locally via markdown_save asFilename=<rel-path>,
which copies it into the current project as a local file.

Two agents in different projects are isolated by virtue of different
project roots. Each project's `.markdown-helper/` is independent.
Two agents in different subdirs of the SAME project are isolated by
their different `dir` offsets.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator


HELPER_DIRNAME = ".markdown-helper"
FOREIGN_PREFIX = "ext/"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
#
# UUID-keyed sections forming a linked list per parent. Top-level sections
# have parent_id IS NULL. Section order within a parent's children is
# given by the prev_sibling_id chain: the first child has prev_sibling_id
# IS NULL, each subsequent child points back to the previous.
#
# title can be NULL: that's a "section 0" preamble (raw prose with no
# heading line). Used for text before any heading in the file.
#
# heading_level is computed at render time from depth in the tree, NOT
# stored. The only "heading-related" decision stored is "is this section
# headed at all?" -- captured by title being NULL or set.

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS sections (
    id              TEXT PRIMARY KEY,        -- UUID, internal only
    parent_id       TEXT,                    -- NULL for top-level
    prev_sibling_id TEXT,                    -- NULL for first child / first top-level
    title           TEXT,                    -- NULL for section-0 preamble
    seed            TEXT,
    content         TEXT,
    state           TEXT NOT NULL,
    word_count      INTEGER NOT NULL DEFAULT 0,
    updated_at      INTEGER NOT NULL,
    updated_by      TEXT,
    session_id      TEXT,
    FOREIGN KEY (parent_id)       REFERENCES sections(id),
    FOREIGN KEY (prev_sibling_id) REFERENCES sections(id)
);

CREATE INDEX IF NOT EXISTS idx_sections_parent      ON sections(parent_id);
CREATE INDEX IF NOT EXISTS idx_sections_prev        ON sections(prev_sibling_id);
CREATE INDEX IF NOT EXISTS idx_sections_state       ON sections(state);

CREATE TABLE IF NOT EXISTS revisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id   TEXT NOT NULL,
    content      TEXT,
    state        TEXT NOT NULL,
    notes        TEXT,
    created_at   INTEGER NOT NULL,
    created_by   TEXT,
    FOREIGN KEY (section_id) REFERENCES sections(id)
);

CREATE INDEX IF NOT EXISTS idx_revisions_section ON revisions(section_id);

CREATE TABLE IF NOT EXISTS assistants (
    job_id       TEXT PRIMARY KEY,
    section_id   TEXT NOT NULL,
    agent        TEXT NOT NULL,
    session_id   TEXT,
    pid          INTEGER,
    state        TEXT NOT NULL,
    started_at   INTEGER NOT NULL,
    ended_at     INTEGER,
    prompt       TEXT,
    log_path     TEXT,
    FOREIGN KEY (section_id) REFERENCES sections(id)
);

CREATE INDEX IF NOT EXISTS idx_assistants_section_state ON assistants(section_id, state);
"""


# ---------------------------------------------------------------------------
# Project root + helper root discovery
# ---------------------------------------------------------------------------

def find_project_root(start: Path | None = None) -> Path:
    """Locate the project root by walking up from `start` (default cwd)
    looking for `.git`. Returns the directory containing `.git`.

    Falls back to `start` itself if no `.git` ancestor is found. The
    project root is the agent's "world": all files, all storage are
    anchored here. Two agents in different projects are isolated by
    virtue of different roots.
    """
    cur = (start if start is not None else Path.cwd()).resolve()
    while cur != cur.parent:
        if (cur / ".git").exists():
            return cur
        cur = cur.parent
    return (start if start is not None else Path.cwd()).resolve()


def find_helper_root(start: Path | None = None) -> Path:
    """Project's `.markdown-helper/` directory. Created on first use.

    Adds `.markdown-helper` to `.git/info/exclude` so the dir doesn't
    pollute git status.
    """
    project = find_project_root(start)
    root = project / HELPER_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    _ensure_in_git_exclude(project, HELPER_DIRNAME)
    return root


# ---------------------------------------------------------------------------
# Agent cwd offset (`dir`) -- the cornerstone of cwd-relative addressing
# ---------------------------------------------------------------------------
#
# The agent's `dir` is the path of cwd relative to the project root. It
# is computed once per process (CLI invocation) and used as the implicit
# prefix for every local filename:
#
#   project_root = ~/src/utils                  (the nearest .git ancestor)
#   cwd          = ~/src/utils/markdown-helper
#   dir          = "markdown-helper"
#
# When an agent in that cwd says filename="test/TEST.md", we read/write
#
#   ~/src/utils/markdown-helper/test/TEST.md                    (disk)
#   ~/src/utils/.markdown-helper/markdown-helper/test/TEST.md/  (workspace)
#
# When the agent's cwd IS the project root, `dir` is the empty string
# and the layout collapses to the original
#
#   <project>/<filename>                          (disk)
#   <project>/.markdown-helper/<filename>/        (workspace)

_DIR_CACHE: tuple[Path, str] | None = None


def get_dir(project_root: Path | None = None) -> str:
    """The agent's cwd offset relative to the project root, in POSIX
    form. Empty string when cwd == project_root, or when cwd cannot be
    expressed relative to project_root (rare; e.g. the CLI was invoked
    from a directory outside the project).

    Cached after first call to keep behavior consistent across the
    process even if cwd changes mid-run (which shouldn't happen, but
    avoiding surprises is cheap).
    """
    global _DIR_CACHE
    if project_root is None:
        project_root = find_project_root()
    project_root = project_root.resolve()
    if _DIR_CACHE is not None and _DIR_CACHE[0] == project_root:
        return _DIR_CACHE[1]
    cwd = Path.cwd().resolve()
    try:
        rel = cwd.relative_to(project_root)
    except ValueError:
        # cwd is outside the project (e.g. test harness tmpdir) -- treat
        # as if cwd == project_root.
        offset = ""
    else:
        offset = "" if rel == Path(".") else rel.as_posix()
        # Reject offsets that land inside the helper dir itself. If
        # cwd is `<project>/.markdown-helper/...`, then naively using
        # the offset would (a) save files into the cache directory
        # and (b) create a nested `<project>/.markdown-helper/.markdown-helper/`
        # workspace tree the next time the helper runs. Neither is
        # ever what the agent wants. Clamp to "" so the agent operates
        # at the project root instead -- the worst that happens is
        # the agent's filenames feel project-rooted instead of cwd-
        # rooted, which is the safe failure mode.
        if offset == HELPER_DIRNAME or offset.startswith(HELPER_DIRNAME + "/"):
            offset = ""
    _DIR_CACHE = (project_root, offset)
    return offset


def reset_dir_cache() -> None:
    """Test hook: drop the cached cwd offset so the next call recomputes.
    Production code never needs this -- cwd is fixed for the life of
    the CLI invocation."""
    global _DIR_CACHE
    _DIR_CACHE = None


def cwd_path(project_root: Path | None = None) -> Path:
    """The agent's cwd within the project: `project_root / dir`.

    This is "where the agent is." Local filenames are read/written
    relative to this path, NOT relative to project_root.
    """
    if project_root is None:
        project_root = find_project_root()
    offset = get_dir(project_root)
    return project_root / offset if offset else project_root


def _ensure_in_git_exclude(git_root: Path, entry: str) -> None:
    """Append `entry` to `<git>/info/exclude` if missing. Best effort."""
    exclude_path = git_root / ".git" / "info" / "exclude"
    if not exclude_path.parent.exists():
        return
    try:
        if not exclude_path.exists():
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            exclude_path.touch()
        try:
            content = exclude_path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        for line in content.splitlines():
            if line.strip() in (entry, "/" + entry):
                return
        with exclude_path.open("a", encoding="utf-8") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(entry + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Filename -> storage path resolution
# ---------------------------------------------------------------------------

def normalize_filename(filename: str) -> str:
    """Normalize a LOCAL filename to its canonical (project-relative
    POSIX) form. Foreign (absolute) paths are rejected -- use
    `resolve_filename` for the high-level entry point that supports
    both local and foreign paths.

    Rules:
      - Reject absolute paths (would escape the project root).
      - Reject `..` components (would also escape).
      - Reject the reserved `ext/` prefix (used internally for foreign
        file workspaces).
      - Strip leading `./`.
      - Use `/` as separator regardless of platform.
    """
    if not filename:
        raise ValueError("filename is required (a relative path like 'docs/README.md')")
    p = Path(filename)
    if p.is_absolute():
        raise ValueError(f"filename must be a relative path, not absolute: {filename!r}")
    parts: list[str] = []
    for part in p.parts:
        if part == ".":
            continue
        if part == "..":
            raise ValueError(f"filename may not contain '..': {filename!r}")
        parts.append(part)
    if not parts:
        raise ValueError(f"filename is empty after normalization: {filename!r}")
    canonical = "/".join(parts)
    if canonical == "ext" or canonical.startswith(FOREIGN_PREFIX):
        raise ValueError(
            f"filename {filename!r} starts with the reserved 'ext/' prefix "
            f"(used internally for foreign file workspaces). Pick a different name."
        )
    return canonical


@dataclass(frozen=True)
class ResolvedFilename:
    """The result of resolving an agent-supplied filename.

    Fields:
      canonical: the agent-visible filename. ONE form per file, used
        everywhere -- responses, subsequent tool calls, error messages.
        - LOCAL: cwd-relative POSIX (e.g. "docs/README.md"). Same form
          the agent typed.
        - FOREIGN: the absolute realpath itself (e.g.
          "/home/ryan/src/other/docs/README.md"). Symlinks are resolved.
          Always starts with "/" so it's unambiguous.
      disk_path: absolute Path to the markdown file on disk. For local
        files, equals cwd_path() / canonical. For foreign, equals the
        canonical itself (it's already absolute).
      workspace_key: internal storage key used to derive the workspace
        cache dir. NEVER exposed to the agent.
        - LOCAL: same as canonical.
        - FOREIGN: "ext/<realpath without leading slash>" -- the on-
          disk encoding under .markdown-helper/<dir>/ext/.
      is_foreign: True if the resolved path is outside the current
        project (i.e. the agent reached across project boundaries).
    """
    canonical: str
    disk_path: Path
    workspace_key: str
    is_foreign: bool


def _to_workspace_key(disk_path: Path) -> str:
    """Encode an absolute realpath as the on-disk workspace key form
    (`ext/<abs without leading slash>`). Internal helper -- agents
    never see this string."""
    abs_posix = PurePosixPath(disk_path.as_posix())
    rel_parts = abs_posix.parts[1:] if abs_posix.is_absolute() else abs_posix.parts
    if not rel_parts:
        raise ValueError(f"path {disk_path!r} has no components for foreign key")
    for part in rel_parts:
        if part in ("", "."):
            raise ValueError(f"path {disk_path!r} has invalid components")
    return FOREIGN_PREFIX + "/".join(rel_parts)


def _from_workspace_key(key: str) -> Path:
    """Decode an `ext/...` workspace key back to an absolute Path.
    Internal helper -- agents never type this form."""
    if not key.startswith(FOREIGN_PREFIX):
        raise ValueError(f"not a foreign workspace key: {key!r}")
    tail = key[len(FOREIGN_PREFIX):]
    if not tail:
        raise ValueError(f"foreign key {key!r} has no path after 'ext/'")
    return Path("/" + tail)


def resolve_filename(
    filename: str,
    *,
    project_root: Path | None = None,
    allow_foreign: bool = True,
) -> ResolvedFilename:
    """High-level filename resolver. Accepts:

      - A cwd-relative path (e.g. "docs/README.md")
      - An absolute path (e.g. "/home/ryan/src/other/docs/README.md")
      - A relative path that escapes the project via `..` (e.g.
        "../../other/docs/X.md") -- treated as foreign once realpath
        lands outside the project root
      - The internal `ext/...` workspace key (back-compat with stored
        keys; not something agents normally type)

    Returns the canonical agent-visible filename + the absolute disk
    path + the internal workspace key + a foreign flag. The single
    rule: ONE canonical form per file. Agents always see the same
    string for the same file regardless of which input form they used.

    Foreign canonicals are ALWAYS the realpath (symlinks resolved)
    starting with "/", so an agent who opens "/tmp/sym/X.md" where
    /tmp/sym -> /tmp/real sees back "/tmp/real/X.md" and uses that
    in every subsequent call.

    `project_root` defaults to find_project_root(). Pass it explicitly
    if you've already computed it (avoids a repeat git walk).

    `allow_foreign=False` rejects anything that resolves outside the
    project -- use this in operations that only make sense for local
    files (e.g. `markdown_create`).
    """
    if not filename:
        raise ValueError(
            "filename is required (a path relative to your cwd like "
            "'docs/README.md', or an absolute path for cross-project "
            "read-only access)"
        )

    # Expand ~ for ergonomics on absolute-style inputs. Has no effect
    # on plain relative paths like "docs/README.md".
    if filename.startswith("~"):
        filename = str(Path(filename).expanduser())

    if project_root is None:
        project_root = find_project_root()
    project_root = project_root.resolve()

    # Decide what absolute path the agent is referring to.
    #
    # (a) absolute -- use as given, then realpath
    # (b) ext/ workspace key -- decode then realpath (back-compat)
    # (c) relative -- join with cwd, then realpath; if that escapes
    #     the project, treat as foreign
    p = Path(filename)
    if p.is_absolute():
        disk_path = p.resolve(strict=False)
    elif (filename == "ext" or filename.startswith(FOREIGN_PREFIX)
          or filename.startswith("./" + FOREIGN_PREFIX)):
        stripped = filename[2:] if filename.startswith("./") else filename
        disk_path = _from_workspace_key(stripped).resolve(strict=False)
    else:
        # Cwd-relative path. Validate before joining: reject empty
        # components but ALLOW `..` so agents can naturally reach
        # sibling projects with relative paths.
        parts: list[str] = []
        for part in p.parts:
            if part == ".":
                continue
            parts.append(part)
        if not parts:
            raise ValueError(f"filename is empty after normalization: {filename!r}")
        # Also reject the reserved ext/ prefix on plain-relative inputs
        # so agents don't shadow the storage-encoded form.
        joined = "/".join(parts)
        if joined == "ext" or joined.startswith(FOREIGN_PREFIX):
            raise ValueError(
                f"filename {filename!r} starts with the reserved 'ext/' prefix "
                f"(used internally for foreign file workspaces). Pick a different name."
            )
        disk_path = (cwd_path(project_root) / Path(*parts)).resolve(strict=False)

    # Decide local vs foreign by whether disk_path is inside the project.
    try:
        rel_to_project = disk_path.relative_to(project_root)
        is_foreign = False
    except ValueError:
        is_foreign = True

    if is_foreign:
        if not allow_foreign:
            raise ValueError(
                f"filename {filename!r} resolves to {disk_path}, which is "
                f"outside the current project ({project_root}). This operation "
                f"only accepts paths inside the project."
            )
        # Canonical is the realpath itself -- always absolute, always
        # the same string for the same file. Workspace key is the on-
        # disk ext/... encoding.
        canonical = str(disk_path)
        workspace_key = _to_workspace_key(disk_path)
        return ResolvedFilename(
            canonical=canonical,
            disk_path=disk_path,
            workspace_key=workspace_key,
            is_foreign=True,
        )

    # Local: canonical is the cwd-relative path the agent would type.
    # Recompute it from the resolved disk_path so all input forms
    # converge to the same string.
    cwd = cwd_path(project_root).resolve()
    try:
        cwd_rel = disk_path.relative_to(cwd)
    except ValueError:
        # disk_path is inside the project but NOT under cwd (e.g. agent
        # at <project>/sub/ typed "../other/X.md" landing in
        # <project>/other/X.md). Express it relative to project root
        # and keep the local flavor -- it's still in the project.
        cwd_rel = disk_path.relative_to(project_root)
        # In that case we have to express it as a path that would
        # round-trip from cwd. The simplest stable form: still
        # cwd-relative (with .. segments). But since we forbid ".." in
        # local canonicals (workspace keys can't contain ..), fall back
        # to expressing canonical as the project-rooted path. Agents
        # will need to use that form going forward; it's still local.
        canonical = cwd_rel.as_posix()
    else:
        canonical = cwd_rel.as_posix()
        if canonical == ".":
            raise ValueError(
                f"filename {filename!r} resolves to the cwd itself, not a file"
            )

    # Final validation: canonical must be safe to use as a workspace
    # path component (no ext/ shadow).
    if canonical == "ext" or canonical.startswith(FOREIGN_PREFIX):
        raise ValueError(
            f"filename {filename!r} resolves to a path under the reserved "
            f"'ext/' prefix; pick a different location."
        )
    # Workspace key for locals is identical to canonical (cwd-relative).
    return ResolvedFilename(
        canonical=canonical,
        disk_path=disk_path,
        workspace_key=canonical,
        is_foreign=False,
    )


def file_dir(root: Path, workspace_key: str) -> Path:
    """Workspace cache directory for a workspace key.

    The `workspace_key` is the INTERNAL on-disk encoding produced by
    resolve_filename:
      - LOCAL: cwd-relative POSIX (same as canonical)
      - FOREIGN: "ext/<absolute path with leading slash dropped>"

    Both flavors are scoped under the agent's cwd offset (`dir`) so
    that two agents in different subdirs of the same project do not
    collide on the same workspace:

      <root>/<dir>/<workspace_key>/                         (local)
      <root>/<dir>/ext/<abs-without-leading-slash>/         (foreign)

    When the agent's cwd IS the project root, `dir` is empty and the
    layout collapses to `<root>/<workspace_key>/`.
    """
    offset = get_dir()
    base = root / offset if offset else root
    if workspace_key.startswith(FOREIGN_PREFIX):
        # Foreign keys bypass normalize_filename (which would reject ext/);
        # validate them lightly here.
        if "/" not in workspace_key or workspace_key.endswith("/"):
            raise ValueError(f"invalid foreign workspace key: {workspace_key!r}")
        return base / workspace_key
    return base / normalize_filename(workspace_key)


def file_db_path(root: Path, workspace_key: str) -> Path:
    return file_dir(root, workspace_key) / "db.sqlite3"


def file_abs_path(project_root: Path, workspace_key: str) -> Path:
    """Absolute path of the file on disk for a workspace key.

    For LOCAL keys, this is `<project_root>/<dir>/<workspace_key>` --
    rooted at the agent's cwd, NOT the project root.

    For FOREIGN keys (ext/...), `project_root` is ignored and the
    absolute path is reconstructed from the encoded path.
    """
    if workspace_key.startswith(FOREIGN_PREFIX):
        return _from_workspace_key(workspace_key)
    return cwd_path(project_root) / normalize_filename(workspace_key)


def file_workspace_exists(root: Path, workspace_key: str) -> bool:
    return file_db_path(root, workspace_key).exists()


def is_foreign_workspace_key(workspace_key: str) -> bool:
    """True iff `workspace_key` is the on-disk foreign encoding."""
    return workspace_key.startswith(FOREIGN_PREFIX)


# Back-compat alias. Older code calls is_foreign_filename(canonical)
# expecting the agent-facing form. The agent-facing canonical for
# foreign files is now an absolute path starting with "/", so the
# detection is "starts with /". For local canonicals it returns False.
def is_foreign_filename(canonical: str) -> bool:
    """True iff `canonical` is the agent-facing form of a foreign file
    (an absolute path starting with '/'). For local cwd-relative
    canonicals, returns False."""
    if not canonical:
        return False
    # Agent-facing canonical: foreign is always an absolute path.
    if canonical.startswith("/"):
        return True
    # Tolerate the old "ext/..." form in case any caller still passes
    # the workspace key by accident.
    return canonical.startswith(FOREIGN_PREFIX)


def workspace_key_to_canonical(workspace_key: str) -> str:
    """Convert an on-disk workspace key back to the agent-facing
    canonical form. For local keys, the canonical IS the key. For
    foreign keys, the canonical is the absolute realpath."""
    if workspace_key.startswith(FOREIGN_PREFIX):
        return str(_from_workspace_key(workspace_key))
    return workspace_key


def list_workspaces(root: Path) -> list[tuple[str, str]]:
    """Enumerate (canonical, workspace_key) pairs for every workspace
    visible from the agent's current cwd.

    Walks `<root>/<dir>/` (NOT the entire helper tree) so an agent in
    `<project>/markdown-helper/` only sees workspaces opened from that
    cwd. Two agents in different subdirs of the same project see
    independent file lists; this is by design (workspaces are scoped
    by cwd to prevent collisions).

    Returned canonicals are agent-facing -- cwd-relative for local
    files, absolute realpath for foreign files. Returned workspace
    keys are the on-disk encoding (same as canonical for local;
    "ext/..." for foreign). Use canonicals when displaying to agents,
    workspace keys when building paths.
    """
    out: list[tuple[str, str]] = []
    offset = get_dir()
    base = root / offset if offset else root
    if not base.exists():
        return out
    for db_file in sorted(base.rglob("db.sqlite3")):
        rel = db_file.parent.relative_to(base).as_posix()
        canonical = workspace_key_to_canonical(rel)
        out.append((canonical, rel))
    return out


# Back-compat: old callers expect a list of strings (the agent-facing
# canonicals). Prefer list_workspaces() in new code.
def list_filenames(root: Path) -> list[str]:
    """List the agent-facing canonical filename of every workspace
    visible from the agent's current cwd. See list_workspaces() for
    the version that also returns the internal workspace key."""
    return [canonical for canonical, _ in list_workspaces(root)]


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

@contextmanager
def open_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with sensible defaults. Ensures the
    schema is initialized on every open (idempotent thanks to IF NOT
    EXISTS).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Meta key/value helpers
# ---------------------------------------------------------------------------

def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else None


# ---------------------------------------------------------------------------
# Schema persistence
# ---------------------------------------------------------------------------
#
# Each workspace's state schema lives in `meta` under the key META_SCHEMA
# as a JSON blob (see states.Schema.to_json/from_json). Survives save
# and reopen-with-workspace; falls back to default on first open of a
# workspace-less file.

META_SCHEMA = "schema"


def get_schema(conn: sqlite3.Connection):
    """Return the workspace's state Schema, or the default if none
    persisted. Imports lazily to avoid circular deps."""
    from .states import Schema
    return Schema.from_json(meta_get(conn, META_SCHEMA))


def set_schema(conn: sqlite3.Connection, schema) -> None:
    """Persist the schema to the meta table. `schema` is a
    states.Schema."""
    meta_set(conn, META_SCHEMA, schema.to_json())


# ---------------------------------------------------------------------------
# Touch registry: a lightweight ledger of files read via source:'disk'
# without opening them for edit.
# ---------------------------------------------------------------------------
#
# Layout:
#
#   <root>/<dir>/<workspace_key>/touched.json
#
# The directory holds touched.json INSTEAD of db.sqlite3. file_workspace_exists
# returns False for these (no DB), but file_touched_exists returns True.
# markdown_list surfaces them with no stateCounts -- the absence of a DB
# is itself the signal "viewed but not opened."
#
# Schema of touched.json:
#
#   {
#     "lastReadAt": <unix ts>,
#     "sha": "<sha256 hex of file bytes at read time>",
#     "sizeBytes": <int>
#   }
#
# A subsequent markdown_open promotes the workspace from touched-only
# to full DB-backed (the touched.json is removed).

TOUCHED_FILENAME = "touched.json"


def file_touched_path(root: Path, workspace_key: str) -> Path:
    """Path to the touched.json marker for a workspace key."""
    return file_dir(root, workspace_key) / TOUCHED_FILENAME


def file_touched_exists(root: Path, workspace_key: str) -> bool:
    """True iff a touched.json exists (and no db.sqlite3 -- mutually
    exclusive in practice; if both somehow exist, db wins)."""
    return file_touched_path(root, workspace_key).exists()


def write_touched_entry(
    root: Path,
    workspace_key: str,
    *,
    sha: str,
    size_bytes: int,
    last_read_at: int,
) -> None:
    """Write/refresh a touched.json marker for a file read via
    source:'disk' without an open workspace.

    Idempotent. If a workspace DB already exists for this key, this is
    a no-op (the DB takes precedence as the source of truth).
    """
    import json as _json
    if file_workspace_exists(root, workspace_key):
        return
    path = file_touched_path(root, workspace_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({
        "lastReadAt": last_read_at,
        "sha": sha,
        "sizeBytes": size_bytes,
    }, separators=(",", ":")), encoding="utf-8")


def read_touched_entry(root: Path, workspace_key: str) -> dict | None:
    """Read a touched.json blob, or None if it doesn't exist or is
    malformed."""
    import json as _json
    path = file_touched_path(root, workspace_key)
    if not path.exists():
        return None
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, _json.JSONDecodeError):
        pass
    return None


def remove_touched_entry(root: Path, workspace_key: str) -> None:
    """Remove the touched.json marker, e.g. when promoting to a full
    workspace via markdown_open."""
    path = file_touched_path(root, workspace_key)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    # Try to remove the empty parent dir too, but don't sweat failures.
    try:
        parent = path.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass


def list_touched_workspaces(root: Path) -> list[tuple[str, str]]:
    """Enumerate (canonical, workspace_key) pairs for every touched-only
    entry visible from cwd (analogous to list_workspaces but for files
    that were read without opening).
    """
    out: list[tuple[str, str]] = []
    offset = get_dir()
    base = root / offset if offset else root
    if not base.exists():
        return out
    for marker in sorted(base.rglob(TOUCHED_FILENAME)):
        # Only count it if there's NO db.sqlite3 alongside (full
        # workspace wins).
        if (marker.parent / "db.sqlite3").exists():
            continue
        rel = marker.parent.relative_to(base).as_posix()
        canonical = workspace_key_to_canonical(rel)
        out.append((canonical, rel))
    return out
