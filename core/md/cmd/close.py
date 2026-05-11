"""markdown_close: drop a file's workspace cache.

Removes <project>/.markdown-helper/<filename>/ -- the section state,
revisions, dispatch logs, etc. The actual file on disk is NOT touched;
only the in-memory editor state is cleared.

Refuses by default if there are running dispatches or unsaved changes.
Pass --force to discard.
"""

from __future__ import annotations

import argparse
import shutil

from . import _common as c
from ..storage import find_helper_root, is_foreign_filename, open_db


def register_parser(sub) -> None:
    p = sub.add_parser("close",
                       help="drop a file's workspace cache (file on disk untouched)")
    p.add_argument("--filename", required=True)
    p.add_argument("--force", action="store_true",
                   help="close even with running dispatches or unsaved changes")
    c.add_pretty(p)
    p.set_defaults(func=cmd_close)


def cmd_close(args: argparse.Namespace) -> int:
    resolved = c.resolve_workspace_or_die(args)
    if resolved is None:
        return 1
    _, ws_dir, filename = resolved

    foreign = is_foreign_filename(filename)
    if not args.force:
        with open_db(ws_dir / "db.sqlite3") as conn:
            running = conn.execute(
                "SELECT COUNT(*) AS c FROM assistants WHERE state IN ('queued', 'running')"
            ).fetchone()["c"]
            staleness = c.save_staleness(conn)
        if running > 0:
            c.emit_error({
                "error": f"{running} dispatch(es) running on {filename!r}; pass force=true to close anyway",
                "hint": "Use markdown_dispatch { kill: true } to terminate them first.",
            })
            return 1
        # Foreign workspaces can't be saved back to their original
        # location, so the unsaved-changes gate is meaningless for them
        # -- skip it. (You can still copy them in via markdown_save
        # asFilename, but if you're closing, you've decided not to.)
        if not foreign and staleness["needsSave"]:
            c.emit_error({
                "error": f"{filename!r} has unsaved changes; call markdown_save first or pass force=true to discard",
                "sectionsChangedSinceSave": staleness["sectionsChangedSinceSave"],
            })
            return 1

    shutil.rmtree(ws_dir, ignore_errors=True)

    # For foreign workspaces, prune empty ext/ parent dirs so the helper
    # tree doesn't accumulate /home/ryan/.../ skeleton chains forever.
    if foreign:
        try:
            helper_root = find_helper_root().resolve()
            parent = ws_dir.parent.resolve()
            while parent != helper_root and parent.exists():
                if any(parent.iterdir()):
                    break
                parent.rmdir()
                parent = parent.parent
        except OSError:
            pass

    c.emit({"filename": filename, "closed": True}, args.pretty)
    return 0
