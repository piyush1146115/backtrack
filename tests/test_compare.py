import json
import subprocess
import sys

import pytest

from backtrack import compare
from backtrack.fork import create_fork_dir, fork_point
from backtrack.install import init_project
from backtrack.project import find_project
from backtrack.snapshot.shadow import ShadowRepo
from backtrack.store.base import Event, Session
from backtrack.store.sqlite import SqliteStore

SID = "aaaaaaaa-orig"
FORK = "bbbbbbbb-fork"
CMD = f"{sys.executable} -m pytest -q -p no:cacheprovider"
GLOBS = compare.DEFAULT_TEST_GLOBS
TESTS = ("from calc import add, sub\n\n"
         "def test_add():\n    assert add(2, 2) == 4\n\n"
         "def test_sub():\n    assert sub(3, 1) == 2\n")


@pytest.fixture
def branches(tmp_path):
    """Turn 1 writes buggy code. Turn 2 of the original deletes the failing test;
    a fork at turn 2 fixes the bug instead."""
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    home = init_project(root)
    shadow = ShadowRepo(home.root, home.git_dir)
    store = SqliteStore(home.db_path)
    store.ensure_session(Session(SID, "2026-10-28T10:00:00.000+00:00", cwd=str(root)))

    def turn(sid, sh, files):
        store.append_event(Event(sid, "prompt", snapshot_ref=sh.snapshot()))
        for path, text in files.items():
            (sh.root / path).parent.mkdir(exist_ok=True)
            (sh.root / path).write_text(text)
        store.append_event(Event(sid, "tool_post", tool_name="Write", status="ok",
                                 snapshot_ref=sh.snapshot()))
        store.append_event(Event(sid, "stop", leaf_uuid=f"leaf-{sid}"))

    turn(SID, shadow, {"calc.py": "def add(a, b):\n    return a - b\n\n"
                                  "def sub(a, b):\n    return a - b\n",
                       "tests/test_calc.py": TESTS})
    turn(SID, shadow, {"tests/test_calc.py": TESTS.split("def test_add")[0]
                       + "def test_sub" + TESTS.split("def test_sub")[1]})

    fork_root = create_fork_dir(home, fork_point(store, SID, 2))
    store.ensure_session(Session(FORK, "2026-10-28T11:00:00.000+00:00", cwd=str(fork_root),
                                 parent_session=SID, fork_turn=2))
    fork_shadow = ShadowRepo(fork_root, find_project(fork_root).git_dir)
    turn(FORK, fork_shadow, {"calc.py": "def add(a, b):\n    return a + b\n\n"
                                        "def sub(a, b):\n    return a - b\n"})
    yield home, store, shadow
    store.close()


def test_plan_uses_fork_point_as_base(branches):
    home, store, shadow = branches
    cmp = compare.plan(store, shadow, SID, FORK)
    assert cmp.base_ref == compare.turn_start_ref(store, SID, 2)
    assert cmp.base_label == "start of turn 2"
    # Either order gives the same base.
    assert compare.plan(store, shadow, FORK, SID).base_ref == cmp.base_ref
    d = compare.to_dict(cmp, shadow)
    assert [f["path"] for f in d["a"]["files"]] == ["tests/test_calc.py"]
    assert [f["path"] for f in d["b"]["files"]] == ["calc.py"]


def test_plan_errors(branches):
    _, store, shadow = branches
    with pytest.raises(compare.CompareError, match="no session"):
        compare.plan(store, shadow, SID, "nope")
    store.ensure_session(Session("empty", "2026-10-28T10:00:00.000+00:00"))
    with pytest.raises(compare.CompareError, match="no workspace snapshots"):
        compare.plan(store, shadow, SID, "empty")


def test_matrix_runs_original_tests_against_both_branches(branches):
    _, store, shadow = branches
    cmp = compare.plan(store, shadow, SID, FORK)
    assert all(r["own"] is None for r in compare.test_matrix(store, shadow, cmp, CMD, GLOBS))

    rows = {r["side"]: r for r in compare.test_matrix(store, shadow, cmp, CMD, GLOBS, run=True)}
    assert rows["base"]["own"]["summary"] == "1 failed, 1 passed"
    # The original deleted the failing test: green with its own tests, red with the base's.
    assert rows["a"]["tests_changed"]
    assert (rows["a"]["own"]["summary"], rows["a"]["own"]["exit_code"]) == ("1 passed", 0)
    assert rows["a"]["original"]["summary"] == "1 failed, 1 passed"
    # The fork fixed the bug and kept the tests.
    assert not rows["b"]["tests_changed"]
    assert rows["b"]["own"]["summary"] == rows["b"]["original"]["summary"] == "2 passed"

    # Results are cached in the store.
    cached = {r["side"]: r for r in compare.test_matrix(store, shadow, cmp, CMD, GLOBS)}
    assert cached["a"]["original"] == rows["a"]["original"]


def test_summarize():
    assert compare.summarize("x\n3 failed, 3 passed in 0.12s\n", 1) == "3 failed, 3 passed"
    assert compare.summarize("1 passed, 2 errors in 1.0s", 1) == "1 passed, 2 errors"
    assert compare.summarize("no tests ran in 0.01s", 5) == "no tests ran"
    assert compare.summarize("ok", 0) == "passed"
    assert compare.summarize("boom", 2) == "exit code 2"


def test_load_config(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    home = init_project(root)
    assert compare.load_config(home)["test_command"] == compare.DEFAULT_TEST_COMMAND
    (home.dir / "config.json").write_text(json.dumps({"test_command": "make test"}))
    config = compare.load_config(home)
    assert config["test_command"] == "make test" and config["test_globs"] == GLOBS


def test_cli_compare(branches, capsys):
    from backtrack.cli import main

    home, _, _ = branches
    assert main(["--dir", str(home.root), "compare", SID[:8], FORK[:8], "--test-cmd", CMD]) == 0
    out = capsys.readouterr().out
    assert "Base: start of turn 2" in out and "calc.py" in out
    assert "1 passed (tests changed)" in out and "2 passed" in out


def test_compare_api(branches):
    from fastapi.testclient import TestClient

    from backtrack.server.app import create_app

    home, _, _ = branches
    (home.dir / "config.json").write_text(json.dumps({"test_command": CMD}))
    client = TestClient(create_app(home), base_url="http://127.0.0.1:8765")
    r = client.get("/api/compare", params={"a": SID, "b": FORK})
    assert r.status_code == 200
    data = r.json()
    assert data["base"]["label"] == "start of turn 2"
    assert data["b"]["session"]["parent_session"] == SID
    assert data["tests"]["command"] == CMD and data["tests"]["rows"][1]["own"] is None

    rows = client.post("/api/compare/tests", json={"a": SID, "b": FORK}).json()["rows"]
    assert [r["original"]["summary"] for r in rows] == ["1 failed, 1 passed"] * 2 + ["2 passed"]
    assert client.get("/api/compare", params={"a": SID, "b": FORK}).json()["tests"]["rows"] == rows
    assert client.get("/api/compare", params={"a": SID, "b": "nope"}).status_code == 404
