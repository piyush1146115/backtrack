"""Comparing two branches of work, typically a session and its fork.

Both sides are compared against their common base: the files at the fork point.
Tests run on each side's final files twice, with its own tests and with the tests
as they were at the base, so a branch can't look green by deleting failing tests.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .fork import agent_env
from .project import Project
from .snapshot.shadow import ShadowRepo
from .store.base import Session, Store, TestRun

DEFAULT_TEST_COMMAND = "uv run pytest -q"
DEFAULT_TEST_GLOBS = ["tests/*", "test/*", "*/tests/*", "test_*.py", "*/test_*.py",
                      "*_test.py", "conftest.py", "*/conftest.py"]
TEST_TIMEOUT = 300
MAX_TEST_OUTPUT = 4000
# The pytest summary line, e.g. "3 failed, 3 passed in 0.12s".
COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)\b")
TIMING_RE = re.compile(r" in [\d.]+s\b")


class CompareError(Exception):
    pass


def load_config(project: Project) -> dict:
    """Settings from .backtrack/config.json, with defaults."""
    config = {"test_command": DEFAULT_TEST_COMMAND, "test_globs": DEFAULT_TEST_GLOBS}
    path = project.dir / "config.json"
    if path.is_file():
        config.update(json.loads(path.read_text()))
    return config


@dataclass
class Side:
    session: Session
    ref: str  # files at the end of the session


@dataclass
class Comparison:
    a: Side
    b: Side
    base_ref: str
    base_label: str


def turn_start_ref(store: Store, session_id: str, turn: int) -> str | None:
    return next((e.snapshot_ref for e in store.get_events(session_id)
                 if e.kind == "prompt" and e.turn == turn), None)


def end_ref(store: Store, session: Session) -> str | None:
    refs = [e.snapshot_ref for e in store.get_events(session.session_id) if e.snapshot_ref]
    if refs:
        return refs[-1]
    if session.parent_session:  # a fork that changed nothing yet
        return turn_start_ref(store, session.parent_session, session.fork_turn)
    return None


def plan(store: Store, shadow: ShadowRepo, a_id: str, b_id: str) -> Comparison:
    sessions = {s.session_id: s for s in store.list_sessions()}
    sides = []
    for sid in (a_id, b_id):
        if sid not in sessions:
            raise CompareError(f"no session {sid}")
        ref = end_ref(store, sessions[sid])
        if ref is None:
            raise CompareError(f"session {sid[:8]} has no workspace snapshots")
        sides.append(Side(sessions[sid], ref))
    a, b = sides
    base, label = None, "common snapshot"
    for parent, child in ((a.session, b.session), (b.session, a.session)):
        if child.parent_session == parent.session_id:
            base = turn_start_ref(store, parent.session_id, child.fork_turn)
            label = f"start of turn {child.fork_turn}"
    base = base or shadow.merge_base(a.ref, b.ref)
    if base is None:
        raise CompareError("the sessions have no snapshot in common")
    return Comparison(a, b, base, label)


def test_files(shadow: ShadowRepo, ref: str, globs: list[str]) -> dict[str, str]:
    return {path: blob for path, blob in shadow.files(ref).items()
            if any(fnmatch.fnmatchcase(path, g) for g in globs)}


def summarize(output: str, exit_code: int) -> str:
    for line in reversed(output.splitlines()):
        counts = COUNT_RE.findall(line)
        if counts and TIMING_RE.search(line):
            return ", ".join(f"{n} {word}" for n, word in counts)
        if "no tests ran" in line:
            return "no tests ran"
    return "passed" if exit_code == 0 else f"exit code {exit_code}"


def run_tests(store: Store, shadow: ShadowRepo, code_ref: str, tests_ref: str, command: str,
              globs: list[str], timeout: int = TEST_TIMEOUT) -> TestRun:
    """Run `command` on code_ref's files with tests_ref's test files, in a temp folder."""
    with tempfile.TemporaryDirectory(prefix="backtrack-tests-") as tmp:
        shadow.export(code_ref, tmp)
        if tests_ref != code_ref:
            for path in test_files(shadow, code_ref, globs):
                os.unlink(Path(tmp, path))
            for path, blob in test_files(shadow, tests_ref, globs).items():
                Path(tmp, path).parent.mkdir(parents=True, exist_ok=True)
                Path(tmp, path).write_bytes(shadow.blob(blob))
        start = time.monotonic()
        try:
            proc = subprocess.run(command, shell=True, cwd=tmp, env=agent_env(),
                                  capture_output=True, text=True, timeout=timeout)
            exit_code, output = proc.returncode, proc.stdout + proc.stderr
            summary = summarize(output, exit_code)
        except subprocess.TimeoutExpired:
            exit_code, output, summary = -1, "", f"timed out after {timeout}s"
    run = TestRun(code_ref, tests_ref, command, exit_code, summary,
                  output[-MAX_TEST_OUTPUT:], int((time.monotonic() - start) * 1000))
    store.add_test_run(run)
    return run


def test_matrix(store: Store, shadow: ShadowRepo, cmp: Comparison, command: str,
                globs: list[str], run: bool = False, rerun: bool = False) -> list[dict]:
    """For base, a and b: test results with their own tests and with the base's tests.

    With run=False only cached results are returned (missing ones are None).
    """
    base_tests = test_files(shadow, cmp.base_ref, globs)
    done: dict[tuple[str, str], TestRun] = {}

    def result(code_ref: str, tests_ref: str) -> TestRun | None:
        key = (code_ref, tests_ref)
        if key not in done:
            cached = None if rerun else store.find_test_run(code_ref, tests_ref, command)
            if cached is None and run:
                cached = run_tests(store, shadow, code_ref, tests_ref, command, globs)
            if cached is None:
                return None
            done[key] = cached
        return done[key]

    rows = []
    for side, ref in (("base", cmp.base_ref), ("a", cmp.a.ref), ("b", cmp.b.ref)):
        tests_changed = test_files(shadow, ref, globs) != base_tests
        own = result(ref, ref)
        original = result(ref, cmp.base_ref) if tests_changed else own
        rows.append({"side": side, "code_ref": ref, "tests_changed": tests_changed,
                     "own": own and asdict(own), "original": original and asdict(original)})
    return rows


def to_dict(cmp: Comparison, shadow: ShadowRepo) -> dict:
    return {
        "base": {"ref": cmp.base_ref, "label": cmp.base_label},
        "a": {"session": asdict(cmp.a.session), "ref": cmp.a.ref,
              "files": shadow.diff_stat(cmp.base_ref, cmp.a.ref)},
        "b": {"session": asdict(cmp.b.session), "ref": cmp.b.ref,
              "files": shadow.diff_stat(cmp.base_ref, cmp.b.ref)},
    }
