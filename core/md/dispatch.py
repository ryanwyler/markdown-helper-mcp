"""Dispatch sub-agents to opencode and reconcile their state.

When the primary calls markdown_dispatch we spawn `opencode run --format
json` as a detached daemon, write its pid + log path into the assistants
table, and flip the section into `dispatched` state. The sub-agent's
markdown_set call back into the workspace flips the section to
`submitted` automatically.

If the sub-agent dies without calling markdown_set, our reconciliation
catches that on the next outline/list call: when the .exit sidecar
shows a finished subprocess but the section is still `dispatched`, we
reset the section to `pending`.

Sub-agent prompts include the section's title + seed + (if revising)
the rejection notes. Sub-agents are told the EXACT markdown_set call
to make at the end.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import sqlite3

from .ids import gen_job_id
from .sections import fetch_section
from .storage import get_schema, meta_get
from .util import now


DISPATCH_LOG_DIRNAME = "dispatch"


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------

def dispatch_log_path(workspace_dir: Path, job_id: str) -> Path:
    return workspace_dir / DISPATCH_LOG_DIRNAME / f"{job_id}.log"


def build_prompt(
    *,
    filename: str,
    section_number: str,
    section_title: str | None,
    seed: str | None,
    is_revision: bool,
    extra_instructions: str | None,
) -> str:
    """Construct the prompt the sub-agent receives.

    The prompt names the EXACT markdown-helper tool calls the sub-agent
    should make so it doesn't have to discover them. Tone is direct and
    bounded -- 'do this one thing, don't touch others.'
    """
    parts: list[str] = []
    parts.append(
        "You are an opencode sub-agent dispatched by the markdown-helper MCP "
        "to write a single section of a markdown file. Stay in scope: "
        "do not modify other sections, do not call markdown_save or "
        "markdown_review."
    )
    parts.append("")
    parts.append(
        "You are running INSIDE the project that owns this file. The "
        "file lives in this project's `.markdown-helper/` workspace -- "
        "you can read project source files (READMEs, code, config) for "
        "accurate facts when filling in the section. Cite real names/paths "
        "from the project, not invented ones."
    )
    parts.append("")
    parts.append(f"File: {filename}")
    parts.append(f"Section: {section_number}"
                 + (f"  -- {section_title}" if section_title else ""))
    parts.append("")
    if seed:
        parts.append("Seed (the contract for what this section must cover):")
        parts.append(f"  {seed}")
        parts.append("")
    if is_revision:
        parts.append(
            "This is a REVISION of a previously-rejected attempt. BEFORE you "
            "write anything, call:"
        )
        parts.append(
            f"  markdown_get {{ filename: {filename!r}, sectionNumber: {section_number!r} }}"
        )
        parts.append(
            "and read the response.history[0].notes -- those are the reviewer's "
            "feedback. Address every point. The previous content is in "
            "response.content if you want to keep some of it."
        )
        parts.append("")
    if extra_instructions:
        parts.append("Additional instructions from the primary:")
        parts.append(f"  {extra_instructions}")
        parts.append("")
    parts.append("When you have written the section, save with:")
    parts.append(
        f"  markdown_set {{ filename: {filename!r}, sectionNumber: {section_number!r}, "
        "content: \"<your prose>\", state: \"submitted\" }"
    )
    parts.append("")
    parts.append(
        "Use the section's parent context (other complete sections in the "
        "file) for tone and consistency by calling markdown_outline + "
        "markdown_get on related sections if needed. Otherwise, write the "
        "section based on the seed alone."
    )
    return "\n".join(parts)


def spawn_opencode_dispatch(
    *,
    prompt: str,
    agent: str,
    model: str | None,
    session_id: str | None,
    log_path: Path,
    cwd: Path,
) -> int:
    """Spawn `opencode run --format json` as a detached daemon.

    Mirrors the runner's double-fork pattern so the subprocess survives
    the CLI invocation. Stdout (the JSON event stream) is captured to
    `log_path`. Returns the spawned pid (read from a `.pid` sidecar
    that the grandchild writes).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch()

    argv = ["opencode", "run", "--format", "json", "--agent", agent]
    argv.extend(["--dir", str(cwd)])
    if model:
        argv.extend(["--model", model])
    if session_id:
        argv.extend(["--session", session_id])
    argv.append(prompt)

    if os.fork() > 0:
        # Parent: wait briefly for the grandchild to write its pid.
        pid_file = log_path.with_suffix(".pid")
        import time
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if pid_file.exists() and pid_file.stat().st_size > 0:
                break
            time.sleep(0.05)
        try:
            return int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return -1

    # First child: detach.
    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    # Grandchild: redirect stdio and exec opencode.
    sys.stdin.close()
    sys.stdout.flush()
    sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_RDONLY)
    out_fd = os.open(str(log_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    err_fd = os.open(str(log_path.with_suffix(".err")),
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    os.dup2(devnull, 0)
    os.dup2(out_fd, 1)
    os.dup2(err_fd, 2)
    os.close(devnull)
    os.close(out_fd)
    os.close(err_fd)

    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=sys.stdout,
        stderr=sys.stderr,
        cwd=str(cwd),
    )
    pid_file = log_path.with_suffix(".pid")
    pid_file.write_text(str(proc.pid))
    rc = proc.wait()
    log_path.with_suffix(".exit").write_text(str(rc))
    os._exit(0)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def read_dispatch_log_session(log_path: Path) -> str | None:
    """Scan opencode's --format json output for the first sessionID.

    opencode emits one JSON object per line; the first one always
    carries `sessionID`.
    """
    if not log_path.exists():
        return None
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = obj.get("sessionID")
                if isinstance(sid, str) and sid.startswith("ses_"):
                    return sid
    except OSError:
        return None
    return None


def reconcile_dispatch_job(conn: sqlite3.Connection, job_id: str) -> None:
    """Read the .exit sidecar + log, update the assistants row in place.

    No-op if the job is already terminal. Cheap to call frequently.
    If the subprocess finished but the section is still `dispatched`,
    reset the section to `pending` (the sub-agent didn't manage to
    call markdown_set).
    """
    row = conn.execute(
        "SELECT * FROM assistants WHERE job_id=?", (job_id,)
    ).fetchone()
    if row is None or row["state"] not in ("queued", "running"):
        return

    log_path = Path(row["log_path"])
    exit_path = log_path.with_suffix(".exit")
    exit_code: int | None = None
    if exit_path.exists():
        try:
            exit_code = int(exit_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            exit_code = None

    # Harvest sessionId from the log (safe to repeat).
    if not row["session_id"]:
        harvested = read_dispatch_log_session(log_path)
        if harvested:
            conn.execute(
                "UPDATE assistants SET session_id=? WHERE job_id=?",
                (harvested, job_id),
            )
            conn.execute(
                "UPDATE sections SET session_id=? WHERE id=?",
                (harvested, row["section_id"]),
            )

    if exit_code is not None:
        new_state = "done" if exit_code == 0 else "failed"
        conn.execute(
            "UPDATE assistants SET state=?, ended_at=? WHERE job_id=?",
            (new_state, now(), job_id),
        )
        # Reset stuck working-state sections to schema's initial state.
        sec = fetch_section(conn, row["section_id"])
        if sec is not None:
            schema = get_schema(conn)
            sd = schema.get(sec.state)
            if sd is not None and sd.working:
                conn.execute(
                    """UPDATE sections SET state=?, updated_at=?, updated_by=?
                       WHERE id=?""",
                    (schema.initial_state(), now(), "dispatch-reconcile", sec.id),
                )


def reconcile_all_dispatched(conn: sqlite3.Connection) -> None:
    """Reconcile every running/queued dispatch in this workspace."""
    rows = conn.execute(
        "SELECT job_id FROM assistants WHERE state IN ('queued', 'running')"
    ).fetchall()
    for r in rows:
        reconcile_dispatch_job(conn, r["job_id"])


# ---------------------------------------------------------------------------
# Kill
# ---------------------------------------------------------------------------

def kill_running_dispatches_for_section(
    conn: sqlite3.Connection, section_id: str,
) -> list[dict[str, Any]]:
    """SIGKILL the process group of any running dispatch on this
    section, mark the rows as `killed`. Returns one dict per killed
    job for caller's response.
    """
    rows = conn.execute(
        """SELECT job_id, pid, session_id FROM assistants
           WHERE section_id=? AND state IN ('queued', 'running')""",
        (section_id,),
    ).fetchall()
    killed: list[dict[str, Any]] = []
    for r in rows:
        pid = r["pid"]
        signaled = False
        if pid and pid > 0:
            try:
                os.killpg(os.getpgid(pid), 9)
                signaled = True
            except (ProcessLookupError, PermissionError):
                signaled = False
        conn.execute(
            "UPDATE assistants SET state=?, ended_at=? WHERE job_id=?",
            ("killed", now(), r["job_id"]),
        )
        killed.append({
            "jobId": r["job_id"],
            "pid": pid,
            "sessionId": r["session_id"],
            "signaled": signaled,
        })
    return killed


# Convenience export for cmd modules.
__all__ = [
    "DISPATCH_LOG_DIRNAME",
    "dispatch_log_path",
    "build_prompt",
    "spawn_opencode_dispatch",
    "reconcile_dispatch_job",
    "reconcile_all_dispatched",
    "read_dispatch_log_session",
    "kill_running_dispatches_for_section",
    "gen_job_id",
]
