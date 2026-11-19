import json

import pytest

from backtrack.importer import import_transcript, transcript_dir
from backtrack.store.sqlite import SqliteStore

SID = "11111111-2222-3333-4444-555555555555"


def rec(type_, uuid, ts, content, **extra):
    r = {"type": type_, "uuid": uuid, "sessionId": SID, "cwd": "/proj",
         "timestamp": f"2026-10-28T10:00:{ts:02d}.000Z", "message": {"content": content}}
    r.update(extra)
    return r


def assistant(uuid, ts, msg_id, blocks, out=10):
    r = rec("assistant", uuid, ts, blocks)
    r["message"].update(id=msg_id, usage={"input_tokens": 1, "output_tokens": out})
    return r


TRANSCRIPT = [
    {"type": "permission-mode", "sessionId": SID, "permissionMode": "default"},
    rec("user", "c1", 0, "<command-name>/model</command-name>"),
    rec("user", "c2", 0, "<local-command-stdout>Set model</local-command-stdout>"),
    rec("user", "p1", 1, "fix the failing test"),
    assistant("a1", 2, "m1", [{"type": "tool_use", "id": "t1", "name": "Bash",
                               "input": {"command": "pytest -q"}}]),
    rec("user", "r1", 3, [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                           "content": "1 failed"}],
        toolUseResult="Error: Exit code 1"),
    assistant("a2", 4, "m2", [{"type": "tool_use", "id": "t2", "name": "Edit",
                               "input": {"file_path": "/proj/cache.py", "old_string": "a",
                                         "new_string": "b"}}]),
    rec("user", "r2", 5, [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}],
        toolUseResult={"type": "update", "filePath": "/proj/cache.py"}),
    assistant("side", 5, "mx", [{"type": "text", "text": "subagent"}], out=999) | {"isSidechain": True},
    assistant("a3", 6, "m3", [{"type": "text", "text": "Fixed it."}]),
    rec("user", "meta", 6, "injected", isMeta=True),
    rec("user", "p2", 7, [{"type": "text", "text": "now push it"}]),
    assistant("a4", 8, "m4", [{"type": "thinking", "thinking": ""}], out=5),
    assistant("a5", 8, "m4", [{"type": "tool_use", "id": "t3", "name": "Bash",
                               "input": {"command": "git push"}}], out=5),
]


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "bt.db")
    yield s
    s.close()


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in TRANSCRIPT) + "\n")
    return path


def test_import(store, transcript):
    assert import_transcript(store, transcript) == SID
    events = store.get_events(SID)
    assert [(e.kind, e.turn, e.tool_name, e.status) for e in events] == [
        ("session_start", 0, None, None),
        ("prompt", 1, None, None),
        ("tool_pre", 1, "Bash", None),
        ("tool_post", 1, "Bash", "failed"),
        ("tool_pre", 1, "Edit", None),
        ("tool_post", 1, "Edit", "ok"),
        ("stop", 1, None, None),
        ("prompt", 2, None, None),
        ("tool_pre", 2, "Bash", None),  # in flight when the transcript ends
        ("stop", 2, None, None),
    ]
    start, prompt, _, failed, _, edit, stop1, prompt2, push, stop2 = events
    assert start.input == {"source": "import"}
    assert prompt.input["prompt"] == "fix the failing test"
    assert prompt.ts == "2026-10-28T10:00:01.000+00:00"
    assert failed.output == "1 failed"
    assert edit.output == "update /proj/cache.py"
    assert stop1.output == "Fixed it." and stop1.leaf_uuid == "a3"
    assert stop1.tokens == {"input": 3, "output": 30, "cache_read": 0, "cache_creation": 0}
    assert prompt2.input["prompt"] == "now push it"
    assert push.irreversible
    assert stop2.tokens["output"] == 5 and stop2.leaf_uuid == "a5"
    assert all(e.snapshot_ref is None for e in events)
    [session] = store.list_sessions()
    assert (session.cwd, session.transcript_path) == ("/proj", str(transcript))


def test_reimport_is_skipped(store, transcript):
    import_transcript(store, transcript)
    assert import_transcript(store, transcript) is None
    assert len(store.get_events(SID)) == 10


def test_empty_transcript_is_skipped(store, tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text('{"type": "permission-mode"}\n')
    assert import_transcript(store, path) is None


def test_transcript_dir_encoding(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from pathlib import Path
    assert transcript_dir(Path("/Users/a.b/my_repo")) == \
        tmp_path / ".claude" / "projects" / "-Users-a-b-my-repo"
