"""Rewinding and forking a session at turn K.

A fork replaces turn K: it starts from the files as they were before turn K and the
conversation up to the end of turn K-1, and sends a new instruction in place of
turn K's prompt. Files live in a worktree of the shadow repo under .backtrack/forks/.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from .importer import transcript_dir
from .install import write_hook_settings
from .project import Project, find_project
from .snapshot.shadow import ShadowRepo
from .store.base import Event, Session, Store, now


class ForkError(Exception):
    pass


@dataclass
class ForkPoint:
    session: Session
    turn: int
    files_ref: str  # workspace snapshot at the start of turn K
    leaf_uuid: str | None  # conversation cut: last message of turn K-1 (None for turn 1)
    irreversible: list[Event]  # outside effects from turn K on, which a rewind can't undo


def fork_point(store: Store, session_id: str, turn: int) -> ForkPoint:
    session = next((s for s in store.list_sessions() if s.session_id == session_id), None)
    if session is None:
        raise ForkError(f"no session {session_id}")
    events = store.get_events(session_id)
    prompt = next((e for e in events if e.kind == "prompt" and e.turn == turn), None)
    if prompt is None:
        raise ForkError(f"session {session_id[:8]} has no turn {turn}")
    if not prompt.snapshot_ref:
        raise ForkError(f"turn {turn} has no workspace snapshot (imported sessions can't be "
                        "rewound: their files were never captured)")
    leaf = None
    if turn > 1:
        stops = [e for e in events if e.kind == "stop" and e.turn == turn - 1 and e.leaf_uuid]
        if not stops:
            raise ForkError(f"turn {turn - 1} has no recorded end, so the conversation "
                            "can't be cut there")
        leaf = stops[-1].leaf_uuid
    irreversible = [e for e in events
                    if e.kind == "tool_pre" and e.irreversible and e.seq > prompt.seq]
    return ForkPoint(session, turn, prompt.snapshot_ref, leaf, irreversible)


def create_fork_dir(home: Project, point: ForkPoint) -> Path:
    """Check out the files at the fork point into a new folder that records into `home`."""
    name = f"{point.session.session_id[:8]}-t{point.turn}-{secrets.token_hex(2)}"
    path = home.forks_dir / name
    home.forks_dir.mkdir(exist_ok=True)
    ShadowRepo(home.root, home.git_dir).add_worktree(path, point.files_ref, f"fork/{name}")
    write_hook_settings(path)
    return path


def fork_prompt(point: ForkPoint, home: Project, fork_root: Path, instruction: str) -> str:
    if point.leaf_uuid is None:
        return instruction  # a fresh session: no earlier messages mention the old folder
    return (
        f"[Backtrack] This conversation was rewound to before turn {point.turn} and forked. "
        f"You are now working in {fork_root}, a copy of {home.root} as it was at that point. "
        f"Earlier messages refer to files under {home.root}: use the same paths under "
        f"{fork_root} instead, and do not modify {home.root}.\n\n{instruction}"
    )


def claude_command(point: ForkPoint, prompt: str, session_id: str, model: str | None,
                   permission_mode: str, allowed_tools: list[str]) -> list[str]:
    cmd = [os.environ.get("BACKTRACK_CLAUDE", "claude"), "-p", prompt,
           "--session-id", session_id, "--output-format", "json",
           "--permission-mode", permission_mode]
    if point.leaf_uuid:
        # Hidden flag (print mode only): resume with messages up to and including leaf.
        cmd += ["--resume", point.session.session_id, "--resume-session-at", point.leaf_uuid,
                "--fork-session"]
    if model:
        cmd += ["--model", model]
    for tool in allowed_tools:
        cmd += ["--allowedTools", tool]
    return cmd


def agent_env() -> dict[str, str]:
    """The environment for the forked agent, without Backtrack's own virtualenv.

    `uv run backtrack` activates Backtrack's venv; left in place, the agent's `python`
    and `uv run` would resolve to Backtrack's interpreter instead of the project's.
    """
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", None)
    if venv:
        venv_bin = os.path.join(venv, "bin")
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if p.rstrip("/") != venv_bin)
    return env


@dataclass
class ForkResult:
    session_id: str
    fork_root: Path
    result: str
    is_error: bool


def prepare_fork(store: Store, home: Project, point: ForkPoint) -> tuple[str, Path]:
    """Create the fork's folder and session. Returns (session id, folder)."""
    fork_root = create_fork_dir(home, point)
    session_id = str(uuid.uuid4())
    # Record the parent link before Claude starts; the hooks only add events to it.
    store.ensure_session(Session(
        session_id=session_id, started_at=now(), cwd=str(fork_root),
        transcript_path=str(transcript_dir(fork_root) / f"{session_id}.jsonl"),
        parent_session=point.session.session_id, fork_turn=point.turn,
    ))
    return session_id, fork_root


def run_fork(store: Store, home: Project, point: ForkPoint, instruction: str,
             model: str | None = None, permission_mode: str = "acceptEdits",
             allowed_tools: list[str] = ()) -> ForkResult:
    session_id, fork_root = prepare_fork(store, home, point)
    return launch_fork(store, home, point, session_id, fork_root, instruction, model,
                       permission_mode, allowed_tools)


def launch_fork(store: Store, home: Project, point: ForkPoint, session_id: str, fork_root: Path,
                instruction: str, model: str | None = None, permission_mode: str = "acceptEdits",
                allowed_tools: list[str] = ()) -> ForkResult:
    """Run Claude Code in a prepared fork folder until it finishes."""
    cmd = claude_command(point, fork_prompt(point, home, fork_root, instruction), session_id,
                         model, permission_mode, list(allowed_tools))
    try:
        proc = subprocess.run(cmd, cwd=fork_root, env=agent_env(), capture_output=True, text=True)
    except OSError as e:  # e.g. claude not installed
        proc = subprocess.CompletedProcess(cmd, 127, "", str(e))
    result, is_error = proc.stderr.strip() or f"claude exited with {proc.returncode}", True
    for line in proc.stdout.splitlines():
        try:
            out = json.loads(line)
        except ValueError:
            continue  # other hooks may print to stdout
        if isinstance(out, dict) and "result" in out:
            result, is_error = out.get("result") or "", bool(out.get("is_error"))
    if is_error:  # so the timeline shows why the fork stopped
        store.append_event(Event(session_id, "error", input={"fork": result[:2000]}))
    return ForkResult(session_id, fork_root, result, is_error)


def restore_turn(store: Store, session_id: str, turn: int) -> tuple[Path, str, str]:
    """Put the session's folder back as it was at the start of `turn`, in place.

    Returns (folder, restored snapshot, snapshot of the files before, for undo).
    """
    point = fork_point(store, session_id, turn)
    cwd = point.session.cwd
    project = find_project(cwd) if cwd and Path(cwd).is_dir() else None
    if project is None:
        raise ForkError(f"the session's folder {point.session.cwd} is not a Backtrack project")
    before = ShadowRepo(project.root, project.git_dir).restore(point.files_ref)
    return project.root, point.files_ref, before
