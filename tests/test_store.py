import multiprocessing
import sqlite3

import pytest

from backtrack.store.base import Event, Session, TestRun
from backtrack.store.sqlite import SqliteStore


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "bt.db")
    yield s
    s.close()


def add_session(store, sid="s1", started="2026-10-28T10:00:00.000+00:00"):
    store.ensure_session(Session(session_id=sid, started_at=started, cwd="/p"))


def test_seq_is_per_session_and_starts_at_1(store):
    add_session(store, "a")
    add_session(store, "b")
    assert store.append_event(Event("a", "session_start")).seq == 1
    assert store.append_event(Event("a", "prompt")).seq == 2
    assert store.append_event(Event("b", "prompt")).seq == 1


def test_turn_increments_on_prompt(store):
    add_session(store)
    kinds = ["session_start", "prompt", "tool_pre", "tool_post", "stop", "prompt", "stop"]
    turns = [store.append_event(Event("s1", k)).turn for k in kinds]
    assert turns == [0, 1, 1, 1, 1, 2, 2]


def test_round_trip(store):
    add_session(store)
    store.append_event(Event(
        "s1", "tool_post", tool_name="Bash", tool_use_id="t1",
        input={"command": "ls"}, output="a\nb", status="ok", snapshot_ref="abc",
        irreversible=True, tokens={"output": 5}, leaf_uuid="u1",
    ))
    [e] = store.get_events("s1")
    assert (e.tool_name, e.input, e.output, e.status) == ("Bash", {"command": "ls"}, "a\nb", "ok")
    assert (e.snapshot_ref, e.irreversible, e.tokens, e.leaf_uuid) == ("abc", True, {"output": 5}, "u1")


def test_get_events_upto_seq(store):
    add_session(store)
    for _ in range(5):
        store.append_event(Event("s1", "tool_pre"))
    assert [e.seq for e in store.get_events("s1", upto_seq=3)] == [1, 2, 3]


def test_events_are_append_only(store):
    add_session(store)
    store.append_event(Event("s1", "prompt"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("UPDATE events SET output = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("DELETE FROM events")


def test_sessions_are_write_once(store):
    add_session(store, started="first")
    add_session(store, started="second")  # ignored, not an error
    [s] = store.list_sessions()
    assert s.started_at == "first"
    with pytest.raises(sqlite3.IntegrityError, match="write-once"):
        store.conn.execute("DELETE FROM sessions")


def test_test_runs_cache_latest_and_append_only(store):
    assert store.find_test_run("a", "b", "pytest") is None
    store.add_test_run(TestRun("a", "b", "pytest", 1, "1 failed", "out1", 10, ts="2026-10-28T10:00"))
    store.add_test_run(TestRun("a", "b", "pytest", 0, "2 passed", "out2", 12, ts="2026-10-28T11:00"))
    store.add_test_run(TestRun("a", "c", "pytest", 0, "other", "", 1))
    run = store.find_test_run("a", "b", "pytest")
    assert (run.exit_code, run.summary, run.duration_ms) == (0, "2 passed", 12)
    assert store.find_test_run("a", "b", "other cmd") is None
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("UPDATE test_runs SET summary = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("DELETE FROM test_runs")


def test_unknown_kind_rejected(store):
    add_session(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.append_event(Event("s1", "bogus"))
    assert store.get_events("s1") == []


def test_list_sessions_aggregates_and_order(store):
    add_session(store, "old", started="2026-10-01T00:00:00.000+00:00")
    add_session(store, "new", started="2026-10-02T00:00:00.000+00:00")
    store.append_event(Event("old", "prompt", ts="2026-10-01T00:00:01.000+00:00",
                             input={"prompt": "fix the bug"}))
    store.append_event(Event("old", "stop", ts="2026-10-01T00:00:02.000+00:00",
                             tokens={"output": 7}))
    store.append_event(Event("old", "prompt", ts="2026-10-01T00:00:03.000+00:00",
                             input={"prompt": "again"}))
    store.append_event(Event("old", "stop", ts="2026-10-01T00:00:04.000+00:00",
                             tokens={"output": 3}))
    sessions = store.list_sessions()
    assert [s.session_id for s in sessions] == ["new", "old"]
    old = sessions[1]
    assert (old.turns, old.events, old.output_tokens, old.first_prompt) == (2, 4, 10, "fix the bug")


def _writer(db, n):
    s = SqliteStore(db)
    for _ in range(n):
        s.append_event(Event("s1", "tool_pre"))
    s.close()


def test_concurrent_appends_have_no_gaps_or_duplicates(tmp_path):
    db = tmp_path / "bt.db"
    s = SqliteStore(db)
    add_session(s)
    procs = [multiprocessing.Process(target=_writer, args=(db, 50)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    assert all(p.exitcode == 0 for p in procs)
    assert [e.seq for e in s.get_events("s1")] == list(range(1, 201))
    s.close()


def test_search(store):
    add_session(store)
    add_session(store, "s2")
    store.append_event(Event("s1", "prompt", input={"prompt": "Remove the caching layer"}))
    store.append_event(Event("s1", "tool_pre", tool_name="Bash", input={"command": "uv run pytest -q"}))
    store.append_event(Event("s1", "tool_post", tool_name="Bash", status="failed",
                             output="FAILED test_revoked_session_expires_after_ttl"))
    store.append_event(Event("s2", "tool_pre", tool_name="Edit",
                             input={"file_path": "/p/sessions/store.py", "new_string": "ttl * 1000"}))
    [hit] = store.search("cache")  # stemmed prefix: matches "caching"
    assert (hit.session_id, hit.seq, hit.turn, hit.kind) == ("s1", 1, 1, "prompt")
    assert "\x01caching\x02" in hit.snippet
    assert [(h.session_id, h.kind) for h in store.search("pytest")] == [("s1", "tool_pre")]
    assert [h.session_id for h in store.search("store.py")] == ["s2"]
    assert [h.kind for h in store.search("revoked")] == ["tool_post"]
    assert [h.tool_name for h in store.search("edit")] == ["Edit"]
    assert store.search("cache pytest") == []  # every word must match the same event
    for odd in ('-q', '"', "a AND (", "NEAR(", "*", "   "):
        store.search(odd)  # FTS syntax in the input is taken literally, never an error


def test_search_index_is_rebuilt_for_older_databases(tmp_path):
    s = SqliteStore(tmp_path / "bt.db")
    add_session(s)
    s.append_event(Event("s1", "prompt", input={"prompt": "flaky deploy"}))
    s.conn.execute("DROP TABLE events_fts")
    s.close()
    s = SqliteStore(tmp_path / "bt.db")
    assert [h.seq for h in s.search("deploy")] == [1]
    s.append_event(Event("s1", "prompt", input={"prompt": "deploy again"}))
    assert len(s.search("deploy")) == 2
    s.close()


def test_irreversible_counts_and_listing(store):
    add_session(store)
    store.append_event(Event("s1", "tool_pre", tool_name="Bash", input={"command": "npm publish"},
                             irreversible=True))
    store.append_event(Event("s1", "tool_post", tool_name="Bash", status="ok", irreversible=True))
    store.append_event(Event("s1", "tool_pre", tool_name="Bash", input={"command": "ls"}))
    [s] = store.list_sessions()
    assert s.irreversible == 1  # counted once per call, not per pre/post
    assert [e.input["command"] for e in store.irreversible_events()] == ["npm publish"]
