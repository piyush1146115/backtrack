import io
import json
import subprocess
import sys
import time

import pytest

from backtrack import audit as audit_mod
from backtrack.audit import HIGH, INFO, MEDIUM, audit_files
from backtrack.cli import main as cli
from backtrack.compare import DEFAULT_TEST_GLOBS
from backtrack.fork import create_fork_dir, fork_point
from backtrack.install import init_project
from backtrack.project import find_project
from backtrack.snapshot.shadow import ShadowRepo
from backtrack.store.base import Event, Session
from backtrack.store.sqlite import SqliteStore

GLOBS = DEFAULT_TEST_GLOBS + ["*.test.js"]
PATH = "tests/test_cache.py"
BEFORE = """from cache import Cache, ttl


def test_get():
    c = Cache()
    c.put("a", 1)
    assert c.get("a") == 1
    assert len(c) == 1


def test_expiry():
    assert ttl(10) == 1000


class TestEvict:
    def test_lru(self):
        self.assertEqual(Cache(1).evict(), "a")
"""


def audit(after, before=BEFORE, path=PATH):
    old = {} if before is None else {path: before}
    new = {} if after is None else {path: after}
    return [(f.kind, f.severity, f.test) for f in audit_files(old, new, GLOBS)]


def test_honest_changes_are_not_flagged():
    assert audit(BEFORE.replace("Cache()", "Cache(size=2)")) == []  # code under test changed
    assert audit(BEFORE + "\n\ndef test_new():\n    assert ttl(1) == 100\n") == []
    renamed = BEFORE.replace("def test_expiry", "def test_ttl_in_ms")
    assert audit(renamed) == []
    moved = BEFORE.replace("def test_expiry():\n    assert ttl(10) == 1000\n", "")
    assert [(f.kind, f.severity) for f in audit_files(
        {PATH: BEFORE}, {PATH: moved, "tests/test_ttl.py": "def test_expiry():\n    assert ttl(10) == 1000\n"},
        GLOBS)] == []
    assert audit(BEFORE.replace("assert len(c) == 1", "assert len(c) == 1\n    assert c")) == []


def test_removed_and_deleted_tests():
    assert audit(BEFORE.replace("def test_expiry():\n    assert ttl(10) == 1000\n", "")) == [
        ("test_removed", HIGH, "test_expiry")]
    assert audit(None) == [("test_file_deleted", HIGH, None)]
    assert audit_files({"tests/conftest.py": "import pytest\n"}, {}, GLOBS)[0].severity == MEDIUM


def test_skips():
    skipped = BEFORE.replace("def test_expiry", "@pytest.mark.skip(reason='flaky')\ndef test_expiry")
    assert audit(skipped) == [("skip_added", HIGH, "test_expiry")]
    xfail = BEFORE.replace("class TestEvict", "@unittest.expectedFailure\nclass TestEvict")
    assert audit(xfail) == [("skip_added", HIGH, "TestEvict::test_lru")]
    inline = BEFORE.replace("    assert ttl(10)", "    pytest.skip('later')\n    assert ttl(10)")
    assert audit(inline) == [("skip_added", HIGH, "test_expiry")]
    whole = "import pytest\npytestmark = pytest.mark.skip\n" + BEFORE
    assert audit(whole) == [("skip_added", HIGH, None)]


def test_weakened_assertions():
    assert audit(BEFORE.replace("    assert len(c) == 1\n", "")) == [("assertions_removed", MEDIUM, "test_get")]
    assert audit(BEFORE.replace("assert ttl(10) == 1000", "ttl(10)")) == [
        ("assertions_removed", HIGH, "test_expiry")]
    assert audit(BEFORE.replace("assert ttl(10) == 1000", "assert True")) == [
        ("assertion_trivial", HIGH, "test_expiry")]
    swallowed = BEFORE.replace("    assert ttl(10) == 1000",
                               "    try:\n        assert ttl(10) == 1000\n    except AssertionError:\n        pass")
    assert audit(swallowed) == [("assertion_swallowed", MEDIUM, "test_expiry")]
    reraised = swallowed.replace("        pass", "        raise")
    assert audit(reraised) == []
    assert audit(BEFORE.replace("ttl(10) == 1000", "ttl(10) >= 1000")) == [
        ("check_loosened", MEDIUM, "test_expiry")]
    assert audit(BEFORE.replace("ttl(10) == 1000", "ttl(10) == 10000")) == [
        ("expected_changed", INFO, "test_expiry")]


def test_excluded_in_config():
    before = '[tool.pytest.ini_options]\naddopts = "-q"\n'
    after = '[tool.pytest.ini_options]\naddopts = "-q --deselect tests/test_cache.py::test_expiry"\n'
    found = audit_files({"pyproject.toml": before}, {"pyproject.toml": after}, GLOBS)
    assert [(f.kind, f.severity) for f in found] == [("tests_excluded", HIGH)]
    assert audit_files({"pyproject.toml": before}, {"pyproject.toml": before + "x = 1\n"}, GLOBS) == []
    conftest = audit_files({"tests/conftest.py": "collect_ignore = []\n"},
                           {"tests/conftest.py": "collect_ignore = ['test_cache.py']\n"}, GLOBS)
    assert [(f.kind, f.detail) for f in conftest] == [("tests_excluded", "added 'test_cache.py' to collect_ignore")]


def test_other_languages():
    js = "test('adds', () => {\n  expect(add(1, 2)).toBe(3);\n});\ntest('subs', () => {\n  expect(sub(3, 1)).toBe(2);\n});\n"
    path = "src/calc.test.js"
    assert audit(js.replace("test('subs'", "test.skip('subs'"), js, path) == [("skip_added", HIGH, None)]
    assert audit(js.replace("test('adds'", "test.only('adds'"), js, path) == [("only_added", MEDIUM, None)]
    assert audit(js.split("test('subs'")[0], js, path) == [
        ("test_removed", HIGH, None), ("assertions_removed", MEDIUM, None)]
    go = "func TestAdd(t *testing.T) {\n\tif add(1, 2) != 3 {\n\t\tt.Fatal(\"bad\")\n\t}\n}\n"
    assert audit(go.replace("{\n\tif", "{\n\tt.Skip()\n\tif"), go, "calc_test.go") == []  # not a test glob
    globs = GLOBS + ["*_test.go"]
    found = audit_files({"calc_test.go": go}, {"calc_test.go": go.replace("{\n\tif", "{\n\tt.Skip()\n\tif")}, globs)
    assert [(f.kind, f.severity) for f in found] == [("skip_added", HIGH)]
    broken = BEFORE.replace("def test_expiry", "@pytest.mark.skip\ndef test_expiry") + "\ndef oops(:\n"
    assert audit(broken) == [("skip_added", HIGH, None)]  # doesn't parse: patterns instead


# --- Sessions -----------------------------------------------------------------------------

SID, FORK = "aaaaaaaa-orig", "bbbbbbbb-fork"
CALC = ("import pytest\nfrom calc import add, sub\n\n"
        "def test_add():\n    assert add(2, 2) == 4\n\ndef test_sub():\n    assert sub(3, 1) == 2\n")
CODE = "def add(a, b):\n    return a {} b\n\ndef sub(a, b):\n    return a - b\n"
CMD = f"{sys.executable} -m pytest -q -p no:cacheprovider"


@pytest.fixture
def session(tmp_path):
    """Turn 1 writes code. Between turns someone edits an expected value.
    Turn 2 skips a test with Edit, turn 3 deletes a test file with Bash.
    A fork at turn 2 fixes the code instead."""
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    home = init_project(root)
    shadow = ShadowRepo(home.root, home.git_dir)
    store = SqliteStore(home.db_path)
    store.ensure_session(Session(SID, "2026-10-29T10:00:00.000+00:00", cwd=str(root)))

    def turn(sid, sh, tool, files, before=None):
        if before:
            (sh.root / before[0]).write_text(before[1])
        store.append_event(Event(sid, "prompt", input={"prompt": "go"}, snapshot_ref=sh.snapshot()))
        for path, text in files.items():
            (sh.root / path).parent.mkdir(exist_ok=True)
            if text is None:
                (sh.root / path).unlink()
            else:
                (sh.root / path).write_text(text)
        store.append_event(Event(sid, "tool_post", tool_name=tool, status="ok", snapshot_ref=sh.snapshot()))
        store.append_event(Event(sid, "stop", leaf_uuid=f"leaf-{sid}"))

    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(CALC)  # the tests were there before the session
    (root / "tests" / "test_more.py").write_text(CALC)
    turn(SID, shadow, "Write", {"calc.py": CODE.format("-")})  # add is wrong: test_add fails
    edited = CALC.replace("== 4", "== 5")
    turn(SID, shadow, "Edit", {"tests/test_calc.py": edited.replace("def test_add", "@pytest.mark.skip\ndef test_add")},
         before=("tests/test_calc.py", edited))
    turn(SID, shadow, "Bash", {"tests/test_more.py": None})

    fork_root = create_fork_dir(home, fork_point(store, SID, 2))
    store.ensure_session(Session(FORK, "2026-10-29T11:00:00.000+00:00", cwd=str(fork_root),
                                 parent_session=SID, fork_turn=2))
    fork_shadow = ShadowRepo(fork_root, find_project(fork_root).git_dir)
    turn(FORK, fork_shadow, "Edit", {"calc.py": CODE.format("+")})
    yield root, store, shadow
    store.close()


def by_id(store, sid):
    return next(s for s in store.list_sessions() if s.session_id == sid)


def test_session_audit_points_at_the_change(session):
    _, store, shadow = session
    a = audit_mod.audit_session(store, shadow, by_id(store, SID), GLOBS)
    found = [(f.kind, f.path, f.test, f.turn, f.tool) for f in a.findings]
    assert found == [
        ("skip_added", "tests/test_calc.py", "test_add", 2, "Edit"),
        ("test_file_deleted", "tests/test_more.py", None, 3, "Bash"),
        ("expected_changed", "tests/test_calc.py", "test_add", 2, None),  # between turns
    ]
    assert a.counts() == {"high": 2, "medium": 0, "info": 1, "intended": 0}
    assert audit_mod.audit_session(store, shadow, by_id(store, FORK), GLOBS).findings == []


def test_session_audit_is_cached(session, monkeypatch):
    _, store, shadow = session
    first = audit_mod.audit_session(store, shadow, by_id(store, SID), GLOBS)
    monkeypatch.setattr(audit_mod, "audit_refs", lambda *a, **k: pytest.fail("not cached"))
    assert audit_mod.audit_session(store, shadow, by_id(store, SID), GLOBS).findings == first.findings


def test_audit_command(session, capsys):
    root, _, _ = session
    assert cli(["--dir", str(root), "audit"]) == 0
    out = capsys.readouterr().out
    assert "aaaaaaaa      2    0    1  go" in out and "1 of 2 sessions changed the tests" in out
    assert cli(["--dir", str(root), "audit", "aaaa"]) == 0
    out = capsys.readouterr().out
    assert "tests/test_calc.py::test_add" in out and "added @pytest.mark.skip  (turn 2, Edit)" in out
    assert "(turn 2, between turns)" in out
    assert cli(["--dir", str(root), "audit", "bbbb"]) == 0
    assert "were not weakened" in capsys.readouterr().out
    assert cli(["--dir", str(root), "audit", "--fail-on", "high"]) == 1
    assert cli(["--dir", str(root), "audit", "bbbb", "--fail-on", "info"]) == 0
    capsys.readouterr()
    assert cli(["--dir", str(root), "audit", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {d["session_id"]: d["counts"]["high"] for d in data} == {SID: 2, FORK: 0}


def test_running_the_original_tests_confirms_the_findings(session, capsys):
    root, store, shadow = session
    sa = audit_mod.audit_session(store, shadow, by_id(store, SID), GLOBS)
    assert audit_mod.audit_tests(store, shadow, sa, CMD, GLOBS) == {
        "command": CMD, "tests_changed": True, "own": None, "original": None, "gamed": False}
    t = audit_mod.audit_tests(store, shadow, sa, CMD, GLOBS, run=True)
    assert t["own"]["exit_code"] == 0 and "skipped" in t["own"]["summary"]
    assert t["original"]["exit_code"] != 0 and t["gamed"]
    assert audit_mod.audit_tests(store, shadow, sa, CMD, GLOBS) == t  # cached

    assert cli(["--dir", str(root), "audit", "aaaa", "--run-tests", "--test-cmd", CMD]) == 0
    assert "only green because it changed the tests" in capsys.readouterr().out


def test_tests_run_in_background_after_a_turn_when_enabled(session, monkeypatch):
    root, store, shadow = session
    from backtrack.hooks import after_turn
    started = []
    monkeypatch.setattr(after_turn, "run_tests_in_background", lambda project, sid: started.append(sid))
    project = find_project(root)
    after_turn.after_turn({"session_id": SID}, project)
    assert started == []
    (project.dir / "config.json").write_text(json.dumps({"audit_run_tests": True}))
    after_turn.after_turn({"session_id": FORK}, project)
    assert started == []  # the fork didn't weaken the tests
    after_turn.after_turn({"session_id": SID}, project)
    assert started == [SID]


def test_background_run_records_the_results(session):
    root, store, shadow = session
    from backtrack.hooks import after_turn
    project = find_project(root)
    (project.dir / "config.json").write_text(json.dumps({"test_command": CMD}))
    after_turn.run_tests_in_background(project, SID)
    sa = audit_mod.audit_session(store, shadow, by_id(store, SID), GLOBS)
    for _ in range(100):
        t = audit_mod.audit_tests(store, shadow, sa, CMD, GLOBS)
        if t["original"]:
            break
        time.sleep(0.1)
    assert t["gamed"], (project.dir / "audit.log").read_text()


def test_marking_findings_as_intended(session, capsys):
    root, store, shadow = session
    assert cli(["--dir", str(root), "audit", "aaaa", "--intended", "1", "2"]) == 0
    out = capsys.readouterr().out
    assert "  1. HIGH" in out and out.count("[marked intended]") == 2
    sa = audit_mod.audit_session(store, shadow, by_id(store, SID), GLOBS)
    assert not sa.weakened() and sa.counts() == {"high": 0, "medium": 0, "info": 1, "intended": 2}
    assert cli(["--dir", str(root), "audit", "--fail-on", "high"]) == 0
    assert cli(["--dir", str(root), "audit", "--fail-on", "info"]) == 1
    assert cli(["--dir", str(root), "audit", "aaaa", "--reopen", "2"]) == 0
    assert cli(["--dir", str(root), "audit", "--fail-on", "high"]) == 1
    with pytest.raises(SystemExit, match="No finding 9"):
        cli(["--dir", str(root), "audit", "aaaa", "--intended", "9"])
    with pytest.raises(SystemExit, match="need a session"):
        cli(["--dir", str(root), "audit", "--intended", "1"])


def test_stop_hook_warns_when_the_turn_weakened_the_tests(session):
    root, store, shadow = session
    from backtrack.hooks import after_turn
    project = find_project(root)
    out = after_turn.after_turn({"session_id": SID}, project)  # turn 3 deleted test_more.py
    assert set(out) == {"systemMessage"}
    assert "tests/test_more.py deleted, with 2 tests" in out["systemMessage"]
    assert "test_calc" not in out["systemMessage"]  # turn 2's skip isn't this turn's
    assert "backtrack audit aaaaaaaa" in out["systemMessage"]
    assert after_turn.after_turn({"session_id": FORK}, project) is None

    (project.dir / "config.json").write_text(json.dumps({"audit_block": True}))
    out = after_turn.after_turn({"session_id": SID}, project)
    assert out["decision"] == "block" and "Put back the tests" in out["reason"]
    again = after_turn.after_turn({"session_id": SID, "stop_hook_active": True}, project)
    assert set(again) == {"systemMessage"}  # blocks once per turn
    (project.dir / "config.json").write_text(json.dumps({"audit_warn": False}))
    assert after_turn.after_turn({"session_id": SID}, project) is None


def test_stop_hook_prints_the_warning(session, monkeypatch, capsys):
    root, store, shadow = session
    from backtrack.hooks import __main__ as hook
    payload = {"hook_event_name": "Stop", "session_id": SID, "cwd": str(root)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hook.main() == 0
    assert "weakened the tests" in json.loads(capsys.readouterr().out)["systemMessage"]
    payload["hook_event_name"] = "SessionEnd"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hook.main() == 0 and capsys.readouterr().out == ""


def test_audit_git_range_names_the_commit(tmp_path, capsys):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t",
                        "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)

    (tmp_path / "tests").mkdir()
    (tmp_path / PATH).write_text(BEFORE)
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-qm", "init")
    git("switch", "-qc", "feat")
    (tmp_path / PATH).write_text(BEFORE.replace("assert ttl(10) == 1000", "pass"))
    git("commit", "-qam", "Simplify expiry")
    (tmp_path / "README").write_text("x")
    git("add", "README")
    git("commit", "-qm", "Readme")

    assert cli(["--dir", str(tmp_path), "audit", "--git", "main..HEAD", "--fail-on", "high"]) == 1
    out = capsys.readouterr().out
    assert "2 commits" in out and "test_expiry" in out and "Simplify expiry)" in out
    assert not (tmp_path / ".backtrack").exists()  # no recording needed

    assert cli(["--dir", str(tmp_path), "audit", "--git", "HEAD~1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["findings"] == []
    with pytest.raises(SystemExit, match="unknown revision"):
        cli(["--dir", str(tmp_path), "audit", "--git", "nope"])
