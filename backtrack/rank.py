"""Ranking the branches that start at one turn: the original from that turn on, and
every fork made from it.

Each branch's final files are tested with its own tests and with the tests at the
fork point, like compare. The ranking puts first the branches that pass the original
tests, then those that pass their own without having changed the tests, then the
smaller diffs and the cheaper runs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import compare
from .snapshot.shadow import ShadowRepo
from .store.base import Event, Session, Store


class RankError(Exception):
    pass


@dataclass
class Branch:
    session: Session
    original: bool  # the session the forks were made from
    ref: str  # files at the end of the branch
    instruction: str  # the branch's prompt at the fork turn
    status: str  # running | done | failed
    model: str | None = None
    output_tokens: int = 0
    duration_ms: int | None = None
    files: list[dict] = field(default_factory=list)
    tests_changed: bool = False
    own: dict | None = None  # TestRun with its own tests
    tests_original: dict | None = None  # TestRun with the tests at the fork point

    @property
    def added(self) -> int:
        return sum(f["added"] or 0 for f in self.files)

    @property
    def deleted(self) -> int:
        return sum(f["deleted"] or 0 for f in self.files)


def _ms(start: str, end: str) -> int:
    return int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000)


def status(events: list[Event]) -> str:
    """running until the last turn has ended; failed if the fork logged an error."""
    if any(e.kind == "error" and isinstance(e.input, dict) and "fork" in e.input for e in events):
        return "failed"
    prompts = [e for e in events if e.kind == "prompt"]
    if not prompts:
        return "running"
    last = prompts[-1]
    ended = any(e.kind in ("stop", "session_end") and e.seq > last.seq for e in events)
    return "done" if ended else "running"


def _branch(store: Store, session: Session, from_turn: int, original: bool, base: str) -> Branch:
    events = store.get_events(session.session_id)
    mine = [e for e in events if (e.turn or 0) >= from_turn]
    prompt = next((e for e in mine if e.kind == "prompt"), None)
    instruction = (prompt.input or {}).get("prompt", "") if prompt else ""
    if not original and instruction.startswith("[Backtrack] ") and "\n\n" in instruction:
        instruction = instruction.split("\n\n", 1)[1]  # drop the note about the fork folder
    refs = [e.snapshot_ref for e in mine if e.snapshot_ref]
    tokens = [e.tokens for e in mine if e.tokens]
    return Branch(
        session=session, original=original, ref=refs[-1] if refs else base,
        instruction=instruction, status=status(events),
        model=next((t["model"] for t in reversed(tokens) if t.get("model")), None),
        output_tokens=sum(t.get("output", 0) for t in tokens),
        duration_ms=_ms(prompt.ts, mine[-1].ts) if prompt else None,
    )


def branches(store: Store, shadow: ShadowRepo, session_id: str, turn: int) -> tuple[str, list[Branch]]:
    """The fork point's files and the branches from turn `turn` of a session."""
    sessions = store.list_sessions()
    parent = next((s for s in sessions if s.session_id == session_id), None)
    if parent is None:
        raise RankError(f"no session {session_id}")
    base = compare.turn_start_ref(store, session_id, turn)
    if base is None:
        raise RankError(f"turn {turn} of {session_id[:8]} has no workspace snapshot")
    out = [_branch(store, parent, turn, True, base)]
    forks = [s for s in sessions if s.parent_session == session_id and s.fork_turn == turn]
    out += [_branch(store, s, 1, False, base) for s in sorted(forks, key=lambda s: s.started_at)]
    for b in out:
        b.files = shadow.diff_stat(base, b.ref)
    return base, out


def add_tests(store: Store, shadow: ShadowRepo, base: str, found: list[Branch], command: str,
              globs: list[str], run: bool = False, rerun: bool = False) -> None:
    """Fill in each finished branch's test results: cached ones, or run the missing ones."""
    base_tests = compare.test_files(shadow, base, globs)
    for b in found:
        if b.status == "running":
            continue  # its files are still changing

        def result(tests_ref: str):
            cached = None if rerun else store.find_test_run(b.ref, tests_ref, command)
            if cached is None and run:
                cached = compare.run_tests(store, shadow, b.ref, tests_ref, command, globs)
            return cached and asdict(cached)

        b.tests_changed = compare.test_files(shadow, b.ref, globs) != base_tests
        b.own = result(b.ref)
        b.tests_original = result(base) if b.tests_changed else b.own


def _passed(run: dict | None) -> bool:
    return bool(run) and run["exit_code"] == 0


def score(b: Branch) -> tuple:
    """Sort key: best first."""
    return (not _passed(b.tests_original),
            not (_passed(b.own) and not b.tests_changed),
            b.status != "done",
            b.added + b.deleted,
            b.output_tokens)


def ranked(found: list[Branch]) -> list[Branch]:
    return sorted(found, key=score)


def to_dict(b: Branch) -> dict:
    return {
        "session": asdict(b.session), "original": b.original, "ref": b.ref,
        "instruction": b.instruction, "status": b.status, "model": b.model,
        "output_tokens": b.output_tokens, "duration_ms": b.duration_ms,
        "files": b.files, "added": b.added, "deleted": b.deleted,
        "tests_changed": b.tests_changed, "own": b.own, "tests_original": b.tests_original,
        "gamed": _passed(b.own) and not _passed(b.tests_original) and b.tests_original is not None,
    }
