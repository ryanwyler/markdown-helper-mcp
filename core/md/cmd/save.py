"""markdown_save: write the workspace state to the file on disk.

Walks the section forest in render order, emits markdown with heading
levels computed from depth, writes to <project>/<filename>.

The workspace IS the source of truth at save time -- save writes
whatever's in the buffer. State machine fields (pending/draft/
submitted/complete/rejected) remain useful for tracking authoring
workflows, but they are NOT a save gate.

ONE GATE: empty-stub leaves. A section that has:
  - title set (it's a heading)
  - content IS NULL or empty (no body)
  - no children (it's a leaf)
is almost certainly an unfilled scaffold from markdown_create. Saving
that produces a heading line with no body, which is a mistake 99% of
the time. The gate fires and refuses to save unless force=true is
passed.

Note: empty parent headings are NOT stubs. A section with empty
content but children is a structural grouping (e.g. `## Endpoints`
followed immediately by `### POST /events`) -- the heading exists to
group its children. Those pass the gate without force.

Updates meta.last_saved_at so the needsSave signal in outline/list
reverts to false until the next section update.

CROSS-PROJECT SAVE: pass --as-filename=<rel-path> to write the doc
to <agent-cwd>/<rel-path> (creating directories if missing) instead
of its original location. asFilename is interpreted relative to the
agent's cwd within the project, NOT the project root, so an agent
in <project>/markdown-helper saying asFilename="docs/copy.md" lands
the file at <project>/markdown-helper/docs/copy.md. This is the
supported way to bring a foreign (read-only) doc into the current
project: the workspace key flips from ext/... to the new local
filename, and the local copy becomes editable.

asFilename also works on local docs as a "save as" -- writes a copy
under the new name and switches the workspace to it. The original
disk file is NOT touched.
"""

from __future__ import annotations

import argparse
import shutil

from . import _common as c
from ..dispatch import reconcile_all_dispatched
from ..render import render_document
from ..sections import fetch_all_sections_in_order, fetch_children
from ..trailer import _is_default_schema, collect_trailer_data, render_trailer
from ..storage import (
    cwd_path,
    file_abs_path,
    file_db_path,
    file_dir,
    file_workspace_exists,
    find_helper_root,
    find_project_root,
    is_foreign_filename,
    meta_set,
    normalize_filename,
    open_db,
)
from ..util import now


def register_parser(sub) -> None:
    p = sub.add_parser("save",
                       help="write the workspace state to <project>/<filename>. "
                            "Pass --as-filename to copy into the current project "
                            "under a new project-relative name (required for "
                            "foreign read-only files).")
    p.add_argument("--filename", required=True)
    p.add_argument("--as-filename", dest="as_filename",
                   help="write to <current-project>/<as-filename> instead of "
                        "the original location, creating directories as "
                        "needed. Switches the workspace key to the new name. "
                        "Required for foreign (read-only, cross-project) files.")
    p.add_argument("--force", action="store_true",
                   help="save even when there are unfilled stub sections "
                        "(headings with no body and no children)")
    c.add_pretty(p)
    p.set_defaults(func=cmd_save)


def cmd_save(args: argparse.Namespace) -> int:
    resolved = c.resolve_workspace_or_die(args)
    if resolved is None:
        return 1
    helper_root, ws_dir, filename = resolved
    is_foreign = is_foreign_filename(filename)

    # asFilename is mandatory for foreign workspaces; for local
    # workspaces it's an optional "save as" rename.
    new_filename: str | None = None
    if args.as_filename:
        try:
            new_filename = normalize_filename(args.as_filename)
        except ValueError as e:
            c.emit_error({
                "error": f"invalid asFilename: {e}",
                "hint": "asFilename must be a project-relative path "
                        "(no leading '/', no '..', no 'ext/' prefix). "
                        "Example: 'docs/copied-from-other-project.md'.",
            })
            return 2
    elif is_foreign:
        c.emit_error({
            "error": f"file {filename!r} was opened from another project (read-only)",
            "readOnly": True,
            "hint": (
                "Pass asFilename=<project-relative path> to copy this doc "
                "into the current project. Example: "
                f"markdown_save {{ filename: {filename!r}, "
                f"asFilename: 'docs/imported.md' }}. "
                "After save, the workspace switches to the local copy "
                "and accepts edits normally."
            ),
        })
        return 1

    with open_db(ws_dir / "db.sqlite3") as conn:
        # Reconcile in case a dispatch finished after we last looked.
        reconcile_all_dispatched(conn)

        sections = fetch_all_sections_in_order(conn)

        # Empty-stub gate: titled leaf sections with no body. These are
        # almost always unfilled scaffolds from markdown_create.
        if not args.force:
            stubs = []
            for s in sections:
                if s.title is None:
                    continue  # headless preamble; not a heading stub
                if s.content and s.content.strip():
                    continue  # has body; not a stub
                if fetch_children(conn, s.id):
                    continue  # has children; structural heading, not a stub
                stubs.append(s)

            if stubs:
                stub_dicts = [c.summarize_section(conn, s) for s in stubs]
                c.emit_error({
                    "error": f"refusing to save -- {len(stubs)} unfilled stub section(s)",
                    "stubs": [
                        {
                            "sectionNumber": d["sectionNumber"],
                            "title": d["title"],
                            "state": d["state"],
                        }
                        for d in stub_dicts
                    ],
                    "hint": "These are headings with no body and no children -- "
                            "almost certainly unfilled scaffolds from markdown_create. "
                            "Either fill them with markdown_set { content }, "
                            "or pass force=true to save anyway (the file will have "
                            "empty heading lines).",
                })
                return 1

        # Build the trailer block + identify archived sections to hide
        # from the rendered markdown. The trailer round-trips per-
        # section state, seeds, tags, and archived subtrees so re-
        # opening this file in a new workspace recovers what the agent
        # established.
        from ..storage import get_schema as _get_schema_save
        schema_for_trailer = _get_schema_save(conn)
        initial_state = schema_for_trailer.initial_state()
        trailer_sections, trailer_archived, skip_uuids = collect_trailer_data(
            conn,
            schema=schema_for_trailer,
            initial_state=initial_state,
        )
        rendered_body = render_document(conn, skip_section_ids=skip_uuids)
        trailer_text = render_trailer(
            trailer_sections, trailer_archived,
            schema=schema_for_trailer,
            schema_is_default=_is_default_schema(schema_for_trailer),
        )
        # The rendered body always ends in a newline; the trailer
        # contributes its own leading blank line (see render_trailer).
        rendered = rendered_body + trailer_text if trailer_text else rendered_body

    # Resolve the on-disk destination.
    project_root = find_project_root()
    if new_filename is not None:
        # Always under the CURRENT project, regardless of source.
        # asFilename is cwd-relative (matches the rest of the filename
        # contract) -- save it under <project_root>/<dir>/<asFilename>,
        # which is "where the agent is" rather than the project root.
        dest_p = (cwd_path(project_root) / new_filename).resolve(strict=False)
        # Refuse to overwrite an existing file: if the agent meant to
        # overwrite, they should markdown_open it instead, then
        # markdown_set / markdown_save.
        if dest_p.exists():
            c.emit_error({
                "error": f"asFilename destination already exists on disk: {dest_p}",
                "hint": "Pick a different asFilename, or markdown_open the "
                        "existing file and edit it directly.",
            })
            return 1
        # Refuse if a workspace already exists under the new key (would
        # collide on rename below).
        if file_workspace_exists(helper_root, new_filename):
            c.emit_error({
                "error": f"asFilename {new_filename!r} already has an open workspace",
                "hint": "Close that workspace first with markdown_close, or "
                        "pick a different asFilename.",
            })
            return 1
    else:
        dest_p = file_abs_path(project_root, filename)

    dest_p.parent.mkdir(parents=True, exist_ok=True)
    dest_p.write_text(rendered, encoding="utf-8")

    # Migrate the workspace cache if asFilename was used.
    final_filename = filename
    final_ws_dir = ws_dir
    if new_filename is not None:
        new_ws_dir = file_dir(helper_root, new_filename)
        new_ws_dir.parent.mkdir(parents=True, exist_ok=True)
        # Move the entire workspace dir atomically. shutil.move handles
        # cross-filesystem fallback to copy+delete if needed.
        shutil.move(str(ws_dir), str(new_ws_dir))
        final_filename = new_filename
        final_ws_dir = new_ws_dir
        # Clean up empty parent dirs left behind under ext/ (best
        # effort -- failures don't affect the save).
        try:
            parent = ws_dir.parent
            stop = helper_root.resolve()
            while parent != stop and parent.exists():
                if any(parent.iterdir()):
                    break
                parent.rmdir()
                parent = parent.parent
        except OSError:
            pass

    with open_db(final_ws_dir / "db.sqlite3") as conn:
        meta_set(conn, "last_saved_at", str(now()))
        if new_filename is not None:
            # The file is now LOCAL to the current project. Clear the
            # foreign flag so mutating commands stop refusing.
            meta_set(conn, c.META_FOREIGN, "0")
            # Flip every section's state from the readonly pseudo-state
            # to `loaded` so writes are accepted (and auto-transition
            # to schema's initial state on first edit). Without this,
            # an agent that just saved-as a foreign doc would still see
            # writes refused with a misleading "do markdown_save
            # asFilename" hint.
            conn.execute(
                "UPDATE sections SET state=?, updated_at=?, updated_by=? "
                "WHERE state=?",
                ("loaded", now(), "save-as", "readonly"),
            )

    # Count states for an informational summary in the response.
    # Re-read sections post-save so save-as foreign->local readonly->loaded
    # flips are reflected. Reusing the pre-save `sections` would lie.
    with open_db(final_ws_dir / "db.sqlite3") as conn:
        sections_after = fetch_all_sections_in_order(conn)
    state_counts: dict[str, int] = {}
    for s in sections_after:
        state_counts[s.state] = state_counts.get(s.state, 0) + 1

    response: dict = {
        "filename": final_filename,
        "destination": str(dest_p),
        "bytes": len(rendered.encode("utf-8")),
        "sectionCount": len(sections),
        "stateCounts": state_counts,
    }

    next_steps: list[str] = []
    if new_filename is not None:
        response["renamedFrom"] = filename
        response["readOnly"] = False
        next_steps.append(
            f"Workspace switched from {filename!r} to {final_filename!r}. "
            f"Use the new filename in subsequent calls; the old key is gone."
        )
        if is_foreign:
            next_steps.append(
                f"This was a foreign (cross-project) doc. The local copy at "
                f"{dest_p} is now writable -- markdown_set / insert / delete / "
                f"move all work normally."
            )

    # If there are non-terminal sections, surface as INFORMATIONAL
    # next-steps -- not a blocking error. Save already succeeded. The
    # determination of "terminal" comes from the doc's declared schema.
    with open_db(final_ws_dir / "db.sqlite3") as conn:
        from ..storage import get_schema as _get_schema
        schema = _get_schema(conn)
    def _is_terminal(state_name: str) -> bool:
        sd = schema.get(state_name)
        return sd is not None and sd.terminal
    non_terminal_states = {
        s.state for s in sections if not _is_terminal(s.state)
    }
    # Drop pseudo-states from the nudge -- those are tool-managed and
    # don't represent agent-pending work.
    non_terminal_states.discard("loaded")
    non_terminal_states.discard("readonly")
    if non_terminal_states:
        n = sum(1 for s in sections if s.state in non_terminal_states)
        next_steps.append(
            f"Saved with {n} non-terminal section(s). "
            f"States: {', '.join(sorted(non_terminal_states))}. "
            f"This is fine -- the workspace is the source of truth at save time."
        )

    if next_steps:
        response["nextSteps"] = next_steps

    c.emit(response, args.pretty)
    return 0
