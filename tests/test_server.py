import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backtrack.install import init_project
from backtrack.server.app import create_app
from backtrack.snapshot.shadow import ShadowRepo
from backtrack.store.base import Event, Session
from backtrack.store.sqlite import SqliteStore

SID = "abcdef12-0000"


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "app.py").write_text("v1\n")
    project = init_project(root)
    shadow = ShadowRepo(project.root, project.git_dir)
    store = SqliteStore(project.db_path)
    store.ensure_session(Session(SID, "2026-10-28T10:00:00.000+00:00", cwd=str(root)))
    r1 = shadow.snapshot()
    store.append_event(Event(SID, "prompt", input={"prompt": "change it"}, snapshot_ref=r1))
    store.append_event(Event(SID, "tool_pre", tool_name="Edit", tool_use_id="t1"))
    (root / "app.py").write_text("v2\n")
    r2 = shadow.snapshot()
    store.append_event(Event(SID, "tool_post", tool_name="Edit", tool_use_id="t1",
                             status="ok", snapshot_ref=r2))
    store.close()
    return TestClient(create_app(project), base_url="http://127.0.0.1:8765"), r1, r2


def test_index_served(setup):
    client, *_ = setup
    r = client.get("/")
    assert r.status_code == 200 and "<title>Backtrack</title>" in r.text


def test_sessions(setup):
    client, *_ = setup
    [s] = client.get("/api/sessions").json()
    assert s["session_id"] == SID and s["turns"] == 1 and s["first_prompt"] == "change it"


def test_session_events_have_prev_snapshot(setup):
    client, r1, r2 = setup
    body = client.get(f"/api/sessions/{SID}").json()
    assert body["session"]["session_id"] == SID
    prompt, pre, post = body["events"]
    assert prompt["prev_snapshot_ref"] is None
    assert "prev_snapshot_ref" not in pre
    assert (post["snapshot_ref"], post["prev_snapshot_ref"]) == (r2, r1)


def test_unknown_session_404(setup):
    client, *_ = setup
    assert client.get("/api/sessions/nope").status_code == 404


def test_diff(setup):
    client, r1, r2 = setup
    diff = client.get("/api/diff", params={"from": r1, "to": r2}).json()["diff"]
    assert "-v1" in diff and "+v2" in diff


def test_diff_bad_and_unknown_refs(setup):
    client, r1, _ = setup
    assert client.get("/api/diff", params={"from": "--help", "to": r1}).status_code == 400
    assert client.get("/api/diff", params={"from": "0" * 40, "to": r1}).status_code == 404


def test_search_and_irreversible(setup):
    client, *_ = setup
    [hit] = client.get("/api/search", params={"q": "change"}).json()
    assert (hit["session_id"], hit["seq"], hit["kind"]) == (SID, 1, "prompt")
    assert "\x01change\x02" in hit["snippet"]
    assert client.get("/api/search", params={"q": "nothing-like-this"}).json() == []
    assert client.get("/api/search", params={"q": "x", "limit": 0}).status_code == 422
    assert client.get("/api/irreversible").json() == []


def test_requests_must_be_local_and_same_origin(setup):
    client, *_ = setup
    assert client.get("/api/sessions", headers={"host": "evil.example:8765"}).status_code == 403
    body = {"session_id": SID, "turn": 1, "mode": "copy"}
    r = client.post("/api/rewind", json=body, headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/rewind", content=json.dumps(body), headers={"content-type": "text/plain"})
    assert r.status_code == 415
    r = client.post("/api/rewind", json=body, headers={"origin": "http://127.0.0.1:8765"})
    assert r.status_code == 200


def test_rewind_restore_copy_and_undo(setup):
    client, r1, r2 = setup
    root = Path(client.get("/api/project").json()["root"])
    r = client.post("/api/rewind", json={"session_id": SID, "turn": 1, "mode": "restore"})
    assert r.status_code == 200 and r.json()["restored"] == r1
    assert (root / "app.py").read_text() == "v1\n"
    r = client.post("/api/undo", json={"session_id": SID, "snapshot": r.json()["undo"]})
    assert r.status_code == 200 and (root / "app.py").read_text() == "v2\n"

    folder = Path(client.post("/api/rewind", json={"session_id": SID, "turn": 1, "mode": "copy"}).json()["folder"])
    assert (folder / "app.py").read_text() == "v1\n" and (root / "app.py").read_text() == "v2\n"

    assert client.post("/api/rewind", json={"session_id": SID, "turn": 7, "mode": "copy"}).status_code == 400
    assert client.post("/api/undo", json={"session_id": SID, "snapshot": "zzz"}).status_code == 400
    assert client.post("/api/undo", json={"session_id": "nope", "snapshot": r1}).status_code == 404


def test_fork_starts_in_background(setup, tmp_path, monkeypatch):
    client, *_ = setup
    log = tmp_path / "args.json"
    fake = tmp_path / "fake-claude"
    fake.write_text(f"#!{sys.executable}\n"
                    "import json, os, sys\n"
                    f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd()}}, open({str(log)!r}, 'w'))\n"
                    "print(json.dumps({'result': 'done', 'is_error': False}))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("BACKTRACK_CLAUDE", str(fake))
    assert client.post("/api/fork", json={"session_id": SID, "turn": 1, "instruction": " "}).status_code == 400

    r = client.post("/api/fork", json={"session_id": SID, "turn": 1, "instruction": "keep it",
                                       "allowed_tools": ["Bash(pytest:*)"]})
    assert r.status_code == 200
    child = r.json()["session_id"]
    sessions = {s["session_id"]: s for s in client.get("/api/sessions").json()}
    assert sessions[child]["parent_session"] == SID and sessions[child]["fork_turn"] == 1
    for t in threading.enumerate():
        if t.name.startswith("fork-"):
            t.join(10)
    call = json.loads(log.read_text())
    assert "keep it" in call["argv"] and "Bash(pytest:*)" in call["argv"]
    assert os.path.realpath(call["cwd"]) == os.path.realpath(r.json()["folder"])


def test_project_install_import_and_config(setup, monkeypatch):
    from backtrack.importer import transcript_dir
    from tests.test_importer import SID as IMPORTED, TRANSCRIPT

    client, *_ = setup
    root = Path(client.get("/api/project").json()["root"])
    monkeypatch.setenv("HOME", str(root.parent / "home"))
    tdir = transcript_dir(root)
    tdir.mkdir(parents=True)
    (tdir / f"{IMPORTED}.jsonl").write_text("\n".join(json.dumps(r) for r in TRANSCRIPT))

    info = client.get("/api/project").json()
    assert info["hooks"] == "missing"  # the fixture only ran init_project
    assert info["transcripts"] == {"dir": str(tdir), "files": 1, "not_recorded": 1}
    assert info["folders"] == [str(root)]
    assert client.post("/api/install", json={}).json()["hooks"] == "installed"

    assert client.post("/api/import", json={}).json() == {"imported": [IMPORTED], "skipped": 0}
    assert client.post("/api/import", json={}).json() == {"imported": [], "skipped": 1}
    assert client.get("/api/project").json()["transcripts"]["not_recorded"] == 0

    r = client.post("/api/config", json={"test_command": "make test", "test_globs": ["t/*", " "]})
    assert r.json()["test_globs"] == ["t/*"]
    info = client.get("/api/project").json()
    assert (info["test_command"], info["test_globs"]) == ("make test", ["t/*"])
    assert client.post("/api/config", json={"test_command": " ", "test_globs": ["x"]}).status_code == 400


def test_restore_points_and_restore_snapshot(setup):
    client, r1, r2 = setup
    root = client.get("/api/project").json()["root"]
    assert client.get("/api/restore-points", params={"folder": root}).json() == []
    undo = client.post("/api/rewind", json={"session_id": SID, "turn": 1, "mode": "restore"}).json()["undo"]
    [point] = client.get("/api/restore-points", params={"folder": root}).json()
    assert (point["ref"], point["restored_to"]) == (undo, r1)
    r = client.post("/api/restore-snapshot", json={"folder": root, "snapshot": undo})
    assert r.status_code == 200 and (Path(root) / "app.py").read_text() == "v2\n"
    assert client.get("/api/restore-points", params={"folder": "/tmp"}).status_code == 400
    assert client.post("/api/restore-snapshot", json={"folder": "/tmp", "snapshot": r1}).status_code == 400
    assert client.post("/api/restore-snapshot", json={"folder": root, "snapshot": "abc"}).status_code == 400


def test_rank_forks_several_ways(setup, tmp_path, monkeypatch):
    client, r1, r2 = setup
    log = tmp_path / "calls"
    log.mkdir()
    fake = tmp_path / "fake-claude"
    fake.write_text(f"#!{sys.executable}\n"
                    "import json, sys, uuid\n"
                    f"json.dump(sys.argv[1:], open({str(log)!r} + '/' + uuid.uuid4().hex, 'w'))\n"
                    "print(json.dumps({'result': 'done', 'is_error': False}))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("BACKTRACK_CLAUDE", str(fake))
    body = {"session_id": SID, "turn": 1, "variants": [{"instruction": " "}]}
    assert client.post("/api/rank/forks", json=body).status_code == 400
    body["variants"] = [{"instruction": "one"}, {"instruction": "two", "model": "haiku"}, {"instruction": ""}]
    r = client.post("/api/rank/forks", json=body)
    assert r.status_code == 200 and len(r.json()["sessions"]) == 2
    for t in threading.enumerate():
        if t.name.startswith("fork-"):
            t.join(10)
    calls = [json.loads(p.read_text()) for p in log.iterdir()]
    assert sorted(c[c.index("-p") + 1] for c in calls) == ["one", "two"]
    assert sum("--model" in c for c in calls) == 1

    ranking = client.get("/api/rank", params={"session_id": SID, "turn": 1}).json()
    assert ranking["base_ref"] == r1
    ids = [b["session"]["session_id"] for b in ranking["branches"]]
    assert set(ids) == {SID, *r.json()["sessions"]}
    orig = next(b for b in ranking["branches"] if b["original"])
    assert orig["ref"] == r2 and orig["files"] == [{"path": "app.py", "added": 1, "deleted": 1}]
    assert client.get("/api/rank", params={"session_id": "nope", "turn": 1}).status_code == 404

    home = client.get("/api/home").json()
    by_id = {s["session_id"]: s for s in home["sessions"]}
    assert by_id[SID]["turn_list"][0]["prompt"] == "change it"
    assert by_id[SID]["status"] == "running"  # turn 1 never stopped
    assert all(by_id[sid]["parent_session"] == SID for sid in r.json()["sessions"])


def test_audit_in_session_home_and_audit_page(tmp_path):
    root = tmp_path / "audited"
    (root / "tests").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "tests" / "test_app.py").write_text("def test_a():\n    assert 1 == 1\n\ndef test_b():\n    assert f() == 2\n")
    project = init_project(root)
    shadow = ShadowRepo(project.root, project.git_dir)
    store = SqliteStore(project.db_path)
    store.ensure_session(Session(SID, "2026-10-29T10:00:00.000+00:00", cwd=str(root)))
    store.append_event(Event(SID, "prompt", input={"prompt": "make it pass"}, snapshot_ref=shadow.snapshot()))
    (root / "tests" / "test_app.py").write_text("def test_a():\n    assert 1 == 1\n")
    store.append_event(Event(SID, "tool_post", tool_name="Edit", status="ok", snapshot_ref=shadow.snapshot()))
    store.close()
    client = TestClient(create_app(project), base_url="http://127.0.0.1:8765")

    audit = client.get(f"/api/sessions/{SID}").json()["audit"]
    assert audit["counts"] == {"high": 1, "medium": 0, "info": 0, "intended": 0}
    [f] = audit["findings"]
    assert (f["kind"], f["test"], f["turn"], f["tool"]) == ("test_removed", "test_b", 1, "Edit")
    [home] = client.get("/api/home").json()["sessions"]
    assert home["audit"]["counts"]["high"] == 1
    [row] = client.get("/api/audit").json()
    assert row["session"]["session_id"] == SID and row["audit"]["findings"][0]["test"] == "test_b"

    review = {"session_id": SID, "kind": "test_removed", "path": "tests/test_app.py", "test": "test_b"}
    updated = client.post("/api/audit/review", json=review | {"verdict": "intended"}).json()
    assert updated["counts"] == {"high": 0, "medium": 0, "info": 0, "intended": 1}
    assert updated["findings"][0]["intended"]
    assert client.get("/api/home").json()["sessions"][0]["audit"]["findings"][0]["intended"]
    assert client.post("/api/audit/review", json=review | {"verdict": "open"}).json()["counts"]["high"] == 1
    assert client.post("/api/audit/review", json=review | {"test": "nope", "verdict": "open"}).status_code == 404
    assert client.post("/api/audit/review", json=review | {"verdict": "maybe"}).status_code == 422


def test_run_audit_tests(tmp_path):
    root = tmp_path / "audited"
    (root / "tests").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "tests" / "test_app.py").write_text("def test_a():\n    assert 1 == 1\n\ndef test_b():\n    assert 1 == 2\n")
    project = init_project(root)
    (project.dir / "config.json").write_text(json.dumps({"test_command": f"{sys.executable} -m pytest -q -p no:cacheprovider"}))
    shadow = ShadowRepo(project.root, project.git_dir)
    store = SqliteStore(project.db_path)
    store.ensure_session(Session(SID, "2026-10-29T10:00:00.000+00:00", cwd=str(root)))
    store.append_event(Event(SID, "prompt", input={"prompt": "make it pass"}, snapshot_ref=shadow.snapshot()))
    (root / "tests" / "test_app.py").write_text("def test_a():\n    assert 1 == 1\n")
    store.append_event(Event(SID, "tool_post", tool_name="Edit", status="ok", snapshot_ref=shadow.snapshot()))
    store.close()
    client = TestClient(create_app(project), base_url="http://127.0.0.1:8765")

    assert client.get(f"/api/sessions/{SID}").json()["audit"]["tests"]["own"] is None
    t = client.post("/api/audit/tests", json={"session_id": SID}).json()
    assert t["own"]["exit_code"] == 0 and t["original"]["exit_code"] != 0 and t["gamed"]
    assert client.get(f"/api/sessions/{SID}").json()["audit"]["tests"]["gamed"]  # cached
    assert client.post("/api/audit/tests", json={"session_id": "nope"}).status_code == 404
