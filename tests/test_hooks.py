import json
import subprocess
import sys
from pathlib import Path

import pytest

from backtrack.install import init_project
from backtrack.store.sqlite import SqliteStore

FIXTURES = Path(__file__).parent / "fixtures"
SID = "833c9da1-05e1-4309-abaf-d834fa8cbd42"


def run_hook(payload, cwd) -> subprocess.CompletedProcess:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, "-m", "backtrack.hooks"], input=raw, cwd=cwd,
                          capture_output=True, text=True)


def load_payloads(project_root: Path, transcript: Path) -> list[dict]:
    text = (FIXTURES / "session_hooks.json").read_text()
    text = text.replace("CWD", str(project_root)).replace("TRANSCRIPT", str(transcript))
    return json.loads(text)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "README").write_text("x\n")
    return init_project(root)


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / "t.jsonl"
    records = [
        {"type": "user", "uuid": "p1", "message": {"content": "Step 1: create hello.py"}},
        {"type": "assistant", "uuid": "a1", "message": {"id": "m1", "content": [],
         "usage": {"input_tokens": 3, "output_tokens": 40}}},
        {"type": "assistant", "uuid": "a2", "message": {"id": "m2", "content": [],
         "usage": {"input_tokens": 2, "output_tokens": 9}}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records))
    return path


def test_full_session_is_recorded(project, transcript):
    for payload in load_payloads(project.root, transcript):
        if payload["hook_event_name"] == "PostToolUse" and payload["tool_name"] == "Write":
            (project.root / "hello.py").write_text("print('hi')\n")  # the tool ran
        result = run_hook(payload, project.root)
        assert result.returncode == 0
        assert result.stdout == ""

    assert not project.error_log.exists()
    store = SqliteStore(project.db_path)
    events = store.get_events(SID)
    assert [(e.seq, e.kind, e.turn, e.tool_name) for e in events] == [
        (1, "session_start", 0, None),
        (2, "prompt", 1, None),
        (3, "tool_pre", 1, "Write"),
        (4, "tool_post", 1, "Write"),
        (5, "tool_pre", 1, "Bash"),
        (6, "tool_post", 1, "Bash"),
        (7, "tool_pre", 1, "Bash"),
        (8, "tool_post", 1, "Bash"),
        (9, "stop", 1, None),
        (10, "session_end", 1, None),
    ]
    prompt, write_post, bash_ok, bash_fail, stop = events[1], events[3], events[5], events[7], events[8]
    assert prompt.input["prompt"].startswith("Step 1") and prompt.snapshot_ref
    assert write_post.status == "ok" and write_post.output == f"create {project.root}/hello.py"
    assert write_post.snapshot_ref != prompt.snapshot_ref  # hello.py was captured
    assert bash_ok.output == "hi" and bash_ok.snapshot_ref == write_post.snapshot_ref
    assert bash_fail.status == "failed" and bash_fail.output.startswith("Exit code 2")
    assert stop.tokens["output"] == 49 and stop.leaf_uuid == "a2"
    [session] = store.list_sessions()
    assert session.cwd == str(project.root) and session.transcript_path == str(transcript)


def test_irreversible_command_flagged(project, transcript):
    payload = {"session_id": SID, "cwd": str(project.root), "hook_event_name": "PreToolUse",
               "tool_name": "Bash", "tool_input": {"command": "git push origin main"},
               "tool_use_id": "t1"}
    run_hook(payload, project.root)
    [event] = SqliteStore(project.db_path).get_events(SID)
    assert event.irreversible


def test_bad_json_fails_open(project):
    result = run_hook("{not json", project.root)
    assert (result.returncode, result.stdout) == (0, "")
    assert "JSONDecodeError" in project.error_log.read_text()


def test_unwritable_store_fails_open(project, transcript):
    project.db_path.unlink()
    project.db_path.mkdir()  # sqlite can't open a directory
    payload = load_payloads(project.root, transcript)[0]
    result = run_hook(payload, project.root)
    assert (result.returncode, result.stdout) == (0, "")
    assert "payload:" in project.error_log.read_text()


def test_uninstalled_folder_is_ignored(tmp_path, transcript):
    payload = load_payloads(tmp_path, transcript)[0]
    result = run_hook(payload, tmp_path)
    assert (result.returncode, result.stdout) == (0, "")
    assert not (tmp_path / ".backtrack").exists()
