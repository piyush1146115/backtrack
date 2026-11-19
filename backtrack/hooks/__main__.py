"""Entry point Claude Code runs for every hook: `python -m backtrack.hooks`.

Reads the hook payload JSON from stdin and appends an event to the project's log.
Fails open: errors go to .backtrack/hook-errors.log, nothing is printed to stdout
(some hooks inject stdout into the agent's context) and the exit code is 0. Two
exceptions: in a fork, writes aimed at the original project are blocked (exit 2), and
the Stop hook prints a warning when the turn weakened the tests (see after_turn).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from ..install import PAUSED_FILE, auto_root, hook_status, init_project
from ..project import Project, find_project
from ..snapshot.shadow import ShadowRepo
from ..store.base import Event, Session, now
from ..store.sqlite import SqliteStore
from ..summarize import MAX_OUTPUT, shrink, summarize_response, truncate
from ..transcript import wait_for_turn_end
from .after_turn import after_turn
from .irreversible import is_irreversible

# Tools that may change files; the workspace is snapshotted after they run.
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
MUTATING_TOOLS = WRITE_TOOLS | {"Bash"}


def build_event(payload: dict, shadow: ShadowRepo) -> Event | None:
    name = payload.get("hook_event_name")
    sid = payload["session_id"]
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")

    if name == "SessionStart":
        return Event(sid, "session_start", input={"source": payload.get("source")})
    if name == "UserPromptSubmit":
        return Event(sid, "prompt",
                     input={"prompt": payload.get("prompt"), "prompt_id": payload.get("prompt_id")},
                     snapshot_ref=shadow.snapshot("turn start"))
    if name == "PreToolUse":
        return Event(sid, "tool_pre", tool_name=tool, tool_use_id=payload.get("tool_use_id"),
                     input=shrink(tool_input), irreversible=is_irreversible(tool, tool_input))
    if name in ("PostToolUse", "PostToolUseFailure"):
        failed = name == "PostToolUseFailure"
        if not failed:
            status, output = "ok", summarize_response(payload.get("tool_response"))
        else:
            status = "interrupted" if payload.get("is_interrupt") else "failed"
            output = truncate(payload.get("error"), MAX_OUTPUT)
        # A failed command can still have changed files, so snapshot either way.
        ref = shadow.snapshot(f"after {tool}") if tool in MUTATING_TOOLS else None
        return Event(sid, "tool_post", tool_name=tool, tool_use_id=payload.get("tool_use_id"),
                     input={"duration_ms": payload.get("duration_ms")}, output=output,
                     status=status, snapshot_ref=ref,
                     irreversible=is_irreversible(tool, tool_input))
    if name == "Stop":
        tokens = leaf = None
        if payload.get("transcript_path") and os.path.exists(payload["transcript_path"]):
            summary = wait_for_turn_end(payload["transcript_path"],
                                        payload.get("last_assistant_message"))
            tokens, leaf = summary.tokens, summary.leaf_uuid
        return Event(sid, "stop", output=truncate(payload.get("last_assistant_message"), MAX_OUTPUT),
                     tokens=tokens, leaf_uuid=leaf)
    if name == "StopFailure":
        details = {k: v for k, v in payload.items()
                   if k not in ("session_id", "transcript_path", "cwd", "hook_event_name")}
        return Event(sid, "error", input=shrink(details))
    if name == "SessionEnd":
        return Event(sid, "session_end", input={"reason": payload.get("reason")})
    return None


def fork_guard(payload: dict, project: Project) -> str | None:
    """In a fork, the reason to block a file write aimed at the original project, if any.

    The forked conversation still mentions the original folder's absolute paths.
    """
    if not project.is_fork or payload.get("hook_event_name") != "PreToolUse" \
            or payload.get("tool_name") not in WRITE_TOOLS:
        return None
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not target:
        return None
    path = Path(target).resolve()
    home, root = project.home.resolve(), project.root.resolve()
    if home in (path, *path.parents) and root not in (path, *path.parents):
        return (f"Backtrack: this session is a fork working in {root}. Do not modify the "
                f"original project at {home}; edit {root / path.relative_to(home)} instead.")
    return None


def record(payload: dict, project: Project, blocked: str | None = None) -> None:
    store = SqliteStore(project.db_path)
    try:
        store.ensure_session(Session(
            session_id=payload["session_id"], started_at=now(),
            cwd=payload.get("cwd"), transcript_path=payload.get("transcript_path"),
        ))
        event = build_event(payload, ShadowRepo(project.root, project.git_dir))
        if event is not None:
            store.append_event(event)
        if blocked:  # the tool never runs, so no PostToolUse will close the call
            store.append_event(Event(payload["session_id"], "tool_post",
                                     tool_name=payload.get("tool_name"),
                                     tool_use_id=payload.get("tool_use_id"),
                                     status="blocked", output=blocked))
    finally:
        store.close()


def log_error(project: Project | None, raw: str) -> None:
    message = (f"--- {datetime.now(timezone.utc).isoformat()}\n"
               f"{traceback.format_exc()}payload: {raw[:2000]}\n")
    if project is None:
        sys.stderr.write(message)
        return
    with open(project.error_log, "a") as f:
        f.write(message)


def auto_project(cwd: str) -> Project | None:
    """For the plugin: the project to record into, set up on first use.

    Folders with their own Backtrack hooks (including forks) are left to those
    hooks, so nothing is recorded twice.
    """
    found = find_project(cwd)
    if found is not None:
        if (found.dir / PAUSED_FILE).exists() or hook_status(found.root) != "missing":
            return None
        if not found.is_fork and not found.git_dir.is_dir():  # e.g. after `backtrack resume`
            init_project(found.root)
        return found
    root, _ = auto_root(cwd)
    return init_project(root) if root else None


def main(auto: bool = False) -> int:
    raw, project, blocked, output = "", None, None, None
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        cwd = payload.get("cwd") or os.getcwd()
        project = auto_project(cwd) if auto else find_project(cwd)
        # None: Backtrack is not set up for this folder.
        if project is not None and not (project.dir / PAUSED_FILE).exists():
            blocked = fork_guard(payload, project)
            record(payload, project, blocked)
            if payload.get("hook_event_name") == "Stop":
                output = after_turn(payload, project)
    except Exception:
        try:
            log_error(project or find_project(os.getcwd()), raw)
        except Exception:
            pass
    if blocked:
        # The one deliberate exception to failing open: exit 2 blocks the tool call
        # and shows the reason to the agent.
        sys.stderr.write(blocked + "\n")
        return 2
    if output:
        print(json.dumps(output))
    return 0


def plugin_main() -> None:
    """`backtrack-hook`, run by the Claude Code plugin: records any git repo."""
    sys.exit(main(auto=True))


if __name__ == "__main__":
    sys.exit(main())
