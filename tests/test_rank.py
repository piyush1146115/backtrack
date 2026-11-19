from backtrack import rank
from backtrack.store.base import Event, Session
from tests.test_compare import CMD, FORK, GLOBS, SID, branches  # noqa: F401 (fixture)


def test_branches_from_a_turn(branches):
    home, store, shadow = branches
    base, found = rank.branches(store, shadow, SID, 2)
    assert base == next(e.snapshot_ref for e in store.get_events(SID) if e.kind == "prompt" and e.turn == 2)
    assert [b.session.session_id for b in found] == [SID, FORK]
    orig, fork = found
    assert orig.original and not fork.original
    assert orig.status == fork.status == "done"
    assert [f["path"] for f in orig.files] == ["tests/test_calc.py"]
    assert [f["path"] for f in fork.files] == ["calc.py"]


def test_tests_and_ranking(branches):
    home, store, shadow = branches
    base, found = rank.branches(store, shadow, SID, 2)
    rank.add_tests(store, shadow, base, found, CMD, GLOBS)
    assert all(b.own is None for b in found)  # nothing cached yet

    rank.add_tests(store, shadow, base, found, CMD, GLOBS, run=True)
    orig, fork = found
    assert orig.tests_changed and orig.own["exit_code"] == 0 and orig.tests_original["exit_code"] != 0
    assert not fork.tests_changed and fork.tests_original["exit_code"] == 0
    assert rank.to_dict(orig)["gamed"] and not rank.to_dict(fork)["gamed"]
    assert [b.session.session_id for b in rank.ranked(found)] == [FORK, SID]


def test_running_and_failed_forks(branches):
    home, store, shadow = branches
    base, _ = rank.branches(store, shadow, SID, 2)
    for sid in ("cccccccc-running", "dddddddd-failed"):
        store.ensure_session(Session(sid, "2026-10-28T12:00:00.000+00:00", parent_session=SID, fork_turn=2))
        store.append_event(Event(sid, "prompt", input={"prompt": "[Backtrack] note\n\ntry this"}))
    store.append_event(Event("dddddddd-failed", "error", input={"fork": "claude exited with 1"}))
    _, found = rank.branches(store, shadow, SID, 2)
    by_id = {b.session.session_id: b for b in found}
    assert by_id["cccccccc-running"].status == "running"
    assert by_id["cccccccc-running"].instruction == "try this"
    assert by_id["cccccccc-running"].ref == base  # no snapshots yet: the fork point's files
    assert by_id["dddddddd-failed"].status == "failed"
    rank.add_tests(store, shadow, base, found, CMD, GLOBS)
    assert by_id["cccccccc-running"].own is None
