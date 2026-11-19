import json
import os
import subprocess
import sys

import pytest

from backtrack.fork import (ForkError, agent_env, claude_command, create_fork_dir, fork_point,
                            fork_prompt, prepare_fork, run_fork)
from backtrack.install import init_project
from backtrack.project import find_project
from backtrack.snapshot.shadow import ShadowRepo
from backtrack.store.base import Event, Session
from backtrack.store.sqlite import SqliteStore

SID = "aaaaaaaa-1111"


@pytest.fixture
def recorded(tmp_path):
    """A project with a recorded 3-turn session; app.py is v1, v2, v3 at each turn start."""
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    home = init_project(root)
    shadow = ShadowRepo(home.root, home.git_dir)
    store = SqliteStore(home.db_path)
    store.ensure_session(Session(SID, "2026-10-28T10:00:00.000+00:00", cwd=str(home.root)))
    refs = {}
    for turn in (1, 2, 3):
        (home.root / "app.py").write_text(f"v{turn}\n")
        refs[turn] = shadow.snapshot()
        store.append_event(Event(SID, "prompt", input={"prompt": f"p{turn}"}, snapshot_ref=refs[turn]))
        if turn == 2:
            store.append_event(Event(SID, "tool_pre", tool_name="Bash",
                                     input={"command": "git push"}, irreversible=True))
        store.append_event(Event(SID, "stop", leaf_uuid=f"leaf{turn}"))
    yield home, store, refs
    store.close()


def test_fork_point(recorded):
    _, store, refs = recorded
    p = fork_point(store, SID, 2)
    assert (p.turn, p.files_ref, p.leaf_uuid) == (2, refs[2], "leaf1")
    assert [e.input["command"] for e in p.irreversible] == ["git push"]
    assert fork_point(store, SID, 3).irreversible == []  # the push was before turn 3


def test_fork_point_turn_1_starts_fresh(recorded):
    _, store, refs = recorded
    p = fork_point(store, SID, 1)
    assert (p.files_ref, p.leaf_uuid) == (refs[1], None)


def test_fork_point_errors(recorded):
    _, store, _ = recorded
    with pytest.raises(ForkError, match="no turn 9"):
        fork_point(store, SID, 9)
    with pytest.raises(ForkError, match="no session"):
        fork_point(store, "nope", 1)
    store.ensure_session(Session("imported", "2026-10-28T10:00:00.000+00:00"))
    store.append_event(Event("imported", "prompt", input={"prompt": "x"}))
    with pytest.raises(ForkError, match="no workspace snapshot"):
        fork_point(store, "imported", 1)


def test_fork_point_needs_end_of_previous_turn(recorded):
    home, store, _ = recorded
    shadow = ShadowRepo(home.root, home.git_dir)
    store.ensure_session(Session("crashed", "2026-10-28T10:00:00.000+00:00"))
    store.append_event(Event("crashed", "prompt", snapshot_ref=shadow.snapshot()))
    store.append_event(Event("crashed", "prompt", snapshot_ref=shadow.snapshot()))
    with pytest.raises(ForkError, match="turn 1 has no recorded end"):
        fork_point(store, "crashed", 2)


def test_create_fork_dir(recorded):
    home, store, _ = recorded
    fork_root = create_fork_dir(home, fork_point(store, SID, 2))
    assert fork_root.parent == home.forks_dir
    assert (fork_root / "app.py").read_text() == "v2\n"
    assert (home.root / "app.py").read_text() == "v3\n"  # original untouched
    settings = json.loads((fork_root / ".claude" / "settings.local.json").read_text())
    assert "backtrack.hooks" in json.dumps(settings)
    assert find_project(fork_root).is_fork


def test_claude_command_and_prompt(recorded, tmp_path):
    home, store, _ = recorded
    p = fork_point(store, SID, 2)
    cmd = claude_command(p, "do it", "new-id", "haiku", "acceptEdits", ["Bash(pytest:*)"])
    assert cmd[1:3] == ["-p", "do it"]
    joined = " ".join(cmd)
    assert f"--resume {SID} --resume-session-at leaf1 --fork-session" in joined
    assert "--session-id new-id" in joined and "--model haiku" in joined
    assert "--permission-mode acceptEdits" in joined and "--allowedTools Bash(pytest:*)" in joined
    assert "--resume" not in claude_command(fork_point(store, SID, 1), "x", "n", None, "default", [])

    text = fork_prompt(p, home, tmp_path / "fork", "keep the cache")
    assert str(tmp_path / "fork") in text and text.endswith("keep the cache")
    assert fork_prompt(fork_point(store, SID, 1), home, tmp_path, "x") == "x"


def test_run_fork_with_fake_claude(recorded, tmp_path, monkeypatch):
    home, store, _ = recorded
    log = tmp_path / "args.json"
    fake = tmp_path / "fake-claude"
    fake.write_text(f"#!{sys.executable}\n"
                    "import json, os, sys\n"
                    f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd()}}, open({str(log)!r}, 'w'))\n"
                    "print('noise from another hook')\n"
                    "print(json.dumps({'result': 'all 6 tests pass', 'is_error': False}))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("BACKTRACK_CLAUDE", str(fake))

    result = run_fork(store, home, fork_point(store, SID, 2), "keep the cache", model="haiku",
                      allowed_tools=["Bash(pytest:*)"])
    assert (result.result, result.is_error) == ("all 6 tests pass", False)
    call = json.loads(log.read_text())
    assert os.path.realpath(call["cwd"]) == str(result.fork_root)
    assert call["argv"][call["argv"].index("--session-id") + 1] == result.session_id
    child = next(s for s in store.list_sessions() if s.session_id == result.session_id)
    assert (child.parent_session, child.fork_turn, child.cwd) == (SID, 2, str(result.fork_root))
    assert child.transcript_path.endswith(f"{result.session_id}.jsonl")


def run_hook(payload, cwd):
    return subprocess.run([sys.executable, "-m", "backtrack.hooks"], input=json.dumps(payload),
                          cwd=cwd, capture_output=True, text=True)


def test_guard_blocks_writes_to_original_project(recorded):
    home, store, _ = recorded
    fork_root = create_fork_dir(home, fork_point(store, SID, 2))

    def pre(path, tool="Edit"):
        return {"session_id": "child", "cwd": str(fork_root), "hook_event_name": "PreToolUse",
                "tool_name": tool, "tool_input": {"file_path": str(path)}, "tool_use_id": f"t-{path}"}

    blocked = run_hook(pre(home.root / "app.py"), fork_root)
    assert blocked.returncode == 2 and blocked.stdout == ""
    assert str(fork_root / "app.py") in blocked.stderr
    assert run_hook(pre(fork_root / "app.py"), fork_root).returncode == 0
    assert run_hook(pre("/tmp/elsewhere.txt"), fork_root).returncode == 0
    assert run_hook(pre(home.root / "app.py", tool="Read"), fork_root).returncode == 0
    # The original project's own sessions are never guarded.
    assert run_hook(pre(home.root / "app.py") | {"cwd": str(home.root)}, home.root).returncode == 0

    events = store.get_events("child")
    assert [(e.kind, e.status) for e in events[:2]] == [("tool_pre", None), ("tool_post", "blocked")]
    assert not home.error_log.exists()


def test_fork_hooks_snapshot_the_fork(recorded):
    home, store, refs = recorded
    fork_root = create_fork_dir(home, fork_point(store, SID, 2))
    (fork_root / "app.py").write_text("fork\n")
    run_hook({"session_id": "child", "cwd": str(fork_root), "hook_event_name": "PostToolUse",
              "tool_name": "Write", "tool_input": {}, "tool_response": {}, "tool_use_id": "t"},
             fork_root)
    [event] = store.get_events("child")
    shadow = ShadowRepo(home.root, home.git_dir)
    assert "+fork" in shadow.diff(refs[2], event.snapshot_ref)


def test_agent_env_drops_backtrack_venv(monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/bt/.venv")
    monkeypatch.setenv("PATH", "/bt/.venv/bin:/usr/local/bin:/usr/bin")
    env = agent_env()
    assert "VIRTUAL_ENV" not in env
    assert env["PATH"] == "/usr/local/bin:/usr/bin"


def test_restore_turn_in_place_and_undo(recorded):
    from backtrack.fork import restore_turn

    home, store, refs = recorded
    (home.root / "app.py").write_text("current\n")
    (home.root / "new.py").write_text("added later\n")
    root, restored, before = restore_turn(store, SID, 2)
    assert (root, restored) == (home.root, refs[2])
    assert (home.root / "app.py").read_text() == "v2\n" and not (home.root / "new.py").exists()
    ShadowRepo(home.root, home.git_dir).restore(before)  # undo
    assert (home.root / "app.py").read_text() == "current\n"
    assert (home.root / "new.py").read_text() == "added later\n"


def test_restore_turn_in_a_fork_restores_the_fork_folder(recorded):
    from backtrack.fork import restore_turn

    home, store, refs = recorded
    session_id, fork_root = prepare_fork(store, home, fork_point(store, SID, 2))
    fork_shadow = ShadowRepo(fork_root, find_project(fork_root).git_dir)
    store.append_event(Event(session_id, "prompt", snapshot_ref=fork_shadow.snapshot()))
    (fork_root / "app.py").write_text("fork edit\n")
    root, _, _ = restore_turn(store, session_id, 1)
    assert root == fork_root and (fork_root / "app.py").read_text() == "v2\n"
    assert (home.root / "app.py").read_text() == "v3\n"  # the original is untouched


def test_failed_fork_is_logged(recorded, monkeypatch):
    home, store, _ = recorded
    monkeypatch.setenv("BACKTRACK_CLAUDE", "/nonexistent/claude")
    result = run_fork(store, home, fork_point(store, SID, 2), "x")
    assert result.is_error
    [event] = store.get_events(result.session_id)
    assert event.kind == "error" and "nonexistent" in event.input["fork"]
