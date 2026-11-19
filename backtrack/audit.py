"""Catching agents that game the tests.

An agent can make failing tests pass without fixing anything: delete them, skip them,
weaken their assertions or exclude them in the test config. The audit reads how the
test files changed between two snapshots, without running anything, and reports each
change that makes the tests check less. Python tests are parsed with `ast`; other
languages are matched with patterns.

Not every finding is cheating: a refactor removes tests too. Each one says what changed
and where, for a reviewer to judge.
"""

from __future__ import annotations

import ast
import difflib
import fnmatch
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from . import compare
from .snapshot.shadow import ShadowRepo
from .store.base import Event, Session, Store

ANALYZER = 1  # bump when the rules change, so cached audits are redone
HIGH, MEDIUM, INFO = "high", "medium", "info"
SEVERITIES = (HIGH, MEDIUM, INFO)

CONFIG_FILES = {"pytest.ini", "setup.cfg", "tox.ini", "pyproject.toml", "package.json"}
CONFIG_PREFIXES = ("jest.config.", "vitest.config.")
# Added config lines that leave tests out of the run.
EXCLUDE_RE = re.compile(r"--deselect|--ignore|(?<![\w-])-k[ =]|testPathIgnorePatterns"
                        r"|-m\s*[\"']?not\b")

SKIP_DECORATORS = {"skip", "skipif", "xfail", "skipIf", "skipUnless", "expectedFailure"}
SKIP_CALLS = {"skip", "xfail", "skipTest"}
STRICT_OPS = {"Eq", "Is"}
OPS = {"Eq": "==", "NotEq": "!=", "Lt": "<", "LtE": "<=", "Gt": ">", "GtE": ">=",
       "Is": "is", "IsNot": "is not", "In": "in", "NotIn": "not in"}

# Other languages, and Python files that don't parse.
GEN_TEST_RE = re.compile(r"^\s*[xf]?(?:it|test)(?:\.(?:skip|only|todo))?\s*\(|\bfunc Test\w*\(|#\[test\]|@Test\b"
                         r"|^\s*(?:async\s+)?def test\w*\(", re.M)
GEN_SKIP_RE = re.compile(r"\b(?:it|test|describe)\.(?:skip|todo)\(|\bx(?:it|test|describe)\("
                         r"|\bt\.Skip(?:Now|f)?\(|#\[ignore\]|@(?:Disabled|Ignore)\b"
                         r"|@pytest\.mark\.(?:skip|xfail)|\bpytest\.skip\(")
GEN_ONLY_RE = re.compile(r"\b(?:it|test|describe)\.only\(|\bf(?:it|describe)\(")
GEN_ASSERT_RE = re.compile(r"\bexpect\(|\bassert\w*!?\(|\bt\.(?:Error|Fatal)f?\(|^\s*assert\b", re.M)


@dataclass
class Finding:
    kind: str  # test_file_deleted | test_removed | skip_added | assertions_removed | ...
    severity: str  # high | medium | info
    path: str
    detail: str
    test: str | None = None
    # Where the change was made, filled in for sessions.
    turn: int | None = None
    seq: int | None = None
    tool: str | None = None  # None: changed between turns, not by a tool call
    intended: bool = False  # a reviewer marked the change as intended

    @property
    def key(self) -> str:
        return f"{self.kind}|{self.path}|{self.test or ''}"


def is_test(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


def is_config(path: str) -> bool:
    name = PurePosixPath(path).name
    return name in CONFIG_FILES or name.startswith(CONFIG_PREFIXES)


# --- Python ---------------------------------------------------------------------------

@dataclass
class PyTest:
    body: str  # ast.dump of the body, to spot renamed and moved tests
    asserts: int = 0
    trivial: int = 0  # assertions that always pass
    swallowed: int = 0  # assertions inside a try whose except doesn't re-raise
    skips: list[str] = field(default_factory=list)
    checks: dict[str, tuple[str, str]] = field(default_factory=dict)  # `assert left op right`


@dataclass
class PyFile:
    tests: dict[str, PyTest]  # "test_x" or "TestClass::test_x"
    skips: list[str]  # module-level skips
    ignores: set[str]  # conftest collect_ignore entries


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return node.id if isinstance(node, ast.Name) else ""


def _skip_marks(decorators: list[ast.expr]) -> list[str]:
    return ["@" + _dotted(d) for d in decorators if _dotted(d).split(".")[-1] in SKIP_DECORATORS]


def _always_true(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
        return ast.dump(node.left) == ast.dump(node.comparators[0])  # assert x == x
    return False


def _counts_assert(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        return True
    if isinstance(node, ast.Call):
        return _dotted(node).split(".")[-1].startswith("assert")  # self.assertEqual, mock.assert_called
    if isinstance(node, ast.With):
        return any(_dotted(i.context_expr).split(".")[-1] == "raises" for i in node.items)
    return False


def _swallows(handler: ast.ExceptHandler) -> bool:
    caught = _dotted(handler.type) if handler.type is not None else ""
    broad = handler.type is None or caught.split(".")[-1] in ("Exception", "BaseException", "AssertionError")
    return broad and not any(isinstance(n, ast.Raise) for n in ast.walk(handler))


def _py_test(fn: ast.FunctionDef | ast.AsyncFunctionDef, inherited: list[str]) -> PyTest:
    t = PyTest(body=ast.dump(ast.Module(body=fn.body, type_ignores=[])),
               skips=inherited + _skip_marks(fn.decorator_list))
    for node in (n for stmt in fn.body for n in ast.walk(stmt)):  # not the decorators
        if _counts_assert(node):
            t.asserts += 1
        if isinstance(node, ast.Assert):
            if _always_true(node.test):
                t.trivial += 1
            elif isinstance(node.test, ast.Compare) and len(node.test.ops) == 1:
                t.checks[ast.unparse(node.test.left)] = (type(node.test.ops[0]).__name__,
                                                         ast.unparse(node.test.comparators[0]))
        elif isinstance(node, ast.Call):
            name = _dotted(node).split(".")
            if name[-1] in ("assertTrue", "assertIsNotNone") and node.args and _always_true(node.args[0]):
                t.trivial += 1
            if name[-1] in SKIP_CALLS and name[0] in ("pytest", "self"):
                t.skips.append(".".join(name) + "()")
        elif isinstance(node, ast.Try) and any(_swallows(h) for h in node.handlers):
            t.swallowed += sum(_counts_assert(n) for b in node.body for n in ast.walk(b))
    return t


def parse_py(text: str) -> PyFile | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    tests, skips, ignores = {}, [], set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            tests[node.name] = _py_test(node, [])
        elif isinstance(node, ast.ClassDef):
            inherited = _skip_marks(node.decorator_list)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    tests[f"{node.name}::{item.name}"] = _py_test(item, inherited)
        elif isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if "pytestmark" in names:
                values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
                skips += ["pytestmark = " + _dotted(v) for v in values
                          if _dotted(v).split(".")[-1] in SKIP_DECORATORS]
            if names & {"collect_ignore", "collect_ignore_glob"} and isinstance(node.value, (ast.List, ast.Tuple)):
                ignores |= {ast.unparse(v) for v in node.value.elts}
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            if _dotted(node.value) == "pytest.skip":
                skips.append("pytest.skip(allow_module_level=True)")
    return PyFile(tests, skips, ignores)


def _compare_tests(path: str, name: str, old: PyTest, new: PyTest) -> list[Finding]:
    out = []
    add = lambda kind, sev, detail: out.append(Finding(kind, sev, path, detail, test=name))
    for mark in [m for m in new.skips if m not in old.skips]:
        add("skip_added", HIGH, f"added {mark}")
    if new.trivial > old.trivial:
        add("assertion_trivial", HIGH, "added an assertion that always passes")
    if new.asserts == 0 < old.asserts:
        add("assertions_removed", HIGH, f"no assertions left (had {old.asserts})")
    elif new.asserts < old.asserts:
        add("assertions_removed", MEDIUM, f"{old.asserts} → {new.asserts} assertions")
    if new.swallowed > old.swallowed:
        add("assertion_swallowed", MEDIUM, "wrapped assertions in a try that swallows failures")
    for left, (op, right) in old.checks.items():
        if left not in new.checks:
            continue
        op2, right2 = new.checks[left]
        if op in STRICT_OPS and op2 not in STRICT_OPS:
            add("check_loosened", MEDIUM, f"assert {left} {OPS[op]} {right} → assert {left} {OPS.get(op2, op2)} {right2}")
        elif op == op2 == "Eq" and right != right2:
            add("expected_changed", INFO, f"expected value of {left}: {right} → {right2}")
    return out


# --- Other languages --------------------------------------------------------------------

def _generic(path: str, old: str | None, new: str | None) -> list[Finding]:
    count = lambda rx, text: len(rx.findall(text or ""))
    tests = count(GEN_TEST_RE, old)
    if new is None:
        return [_deleted(path, tests)]
    out = []
    if count(GEN_TEST_RE, new) < tests:
        out.append(Finding("test_removed", HIGH, path, f"{tests} → {count(GEN_TEST_RE, new)} tests"))
    if count(GEN_SKIP_RE, new) > count(GEN_SKIP_RE, old):
        out.append(Finding("skip_added", HIGH, path, f"{count(GEN_SKIP_RE, new) - count(GEN_SKIP_RE, old)} more skipped"))
    if count(GEN_ONLY_RE, new) > count(GEN_ONLY_RE, old):
        out.append(Finding("only_added", MEDIUM, path, "added .only, which skips every other test"))
    if count(GEN_ASSERT_RE, new) < count(GEN_ASSERT_RE, old):
        out.append(Finding("assertions_removed", MEDIUM, path,
                           f"{count(GEN_ASSERT_RE, old)} → {count(GEN_ASSERT_RE, new)} assertions"))
    return out


def _deleted(path: str, tests: int) -> Finding:
    if tests:
        return Finding("test_file_deleted", HIGH, path, f"deleted, with {tests} test{'s' * (tests != 1)}")
    return Finding("test_file_deleted", MEDIUM, path, "deleted a test helper")


def _config(path: str, old: str | None, new: str | None) -> list[Finding]:
    added = [l[2:] for l in difflib.ndiff((old or "").splitlines(), (new or "").splitlines())
             if l.startswith("+ ")]
    return [Finding("tests_excluded", HIGH, path, f"added `{line.strip()}`")
            for line in added if EXCLUDE_RE.search(line)]


# --- Entry points -----------------------------------------------------------------------

def audit_files(before: dict[str, str], after: dict[str, str], globs: list[str]) -> list[Finding]:
    """Findings for the files that changed: {path: text} before and after (absent: no file)."""
    out: list[Finding] = []
    paths = sorted(before.keys() | after.keys())
    for path in paths:
        if is_config(path) and not is_test(path, globs):
            out += _config(path, before.get(path), after.get(path))

    parsed = {}  # (side, path) -> PyFile; Python test files that parse
    for path in paths:
        if not is_test(path, globs):
            continue
        if path.endswith(".py"):
            sides = [(side, text) for side, text in (("old", before.get(path)), ("new", after.get(path)))
                     if text is not None]
            files = {side: parse_py(text) for side, text in sides}
            if all(f is not None for f in files.values()):
                parsed.update({(side, path): f for side, f in files.items()})
                continue
        out += _generic(path, before.get(path), after.get(path))

    old_tests = {(p, n): t for (s, p), f in parsed.items() if s == "old" for n, t in f.tests.items()}
    new_tests = {(p, n): t for (s, p), f in parsed.items() if s == "new" for n, t in f.tests.items()}
    unmatched = {k for k in new_tests if k not in old_tests}
    removed: dict[str, list[str]] = {}
    for key, old in old_tests.items():
        new_key = key if key in new_tests else next(
            (k for k in sorted(unmatched) if new_tests[k].body == old.body), None)  # renamed or moved
        if new_key is None:
            removed.setdefault(key[0], []).append(key[1])
            continue
        unmatched.discard(new_key)
        out += _compare_tests(new_key[0], new_key[1], old, new_tests[new_key])

    for path in paths:
        old, new = parsed.get(("old", path)), parsed.get(("new", path))
        if old is None and new is None:
            continue
        if new is None and path not in after:
            out.append(_deleted(path, len(removed.get(path, []))) if removed.get(path) or not old.tests
                       else Finding("test_file_deleted", INFO, path, "deleted; its tests moved elsewhere"))
            continue
        out += [Finding("test_removed", HIGH, path, "removed", test=name) for name in removed.get(path, [])]
        if new is not None:
            for mark in [m for m in new.skips if old is None or m not in old.skips]:
                out.append(Finding("skip_added", HIGH, path, f"added {mark} to the whole file"))
            for entry in sorted(new.ignores - (old.ignores if old else set())):
                out.append(Finding("tests_excluded", HIGH, path, f"added {entry} to collect_ignore"))
    return sorted(out, key=lambda f: (SEVERITIES.index(f.severity), f.path, f.test or ""))


def changed_files(shadow, from_ref: str, to_ref: str, globs: list[str],
                  listing: dict | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """The test and test config files that differ between two snapshots, as text.

    `listing` caches each snapshot's file list, for audits over many snapshots."""
    listing = {} if listing is None else listing
    for ref in (from_ref, to_ref):
        if ref not in listing:
            listing[ref] = shadow.files(ref)
    old, new = listing[from_ref], listing[to_ref]
    paths = [p for p in old.keys() | new.keys()
             if old.get(p) != new.get(p) and (is_test(p, globs) or is_config(p))]
    text = lambda files, p: shadow.blob(files[p]).decode("utf-8", "replace")
    return ({p: text(old, p) for p in paths if p in old}, {p: text(new, p) for p in paths if p in new})


def audit_refs(shadow, from_ref: str, to_ref: str, globs: list[str], listing: dict | None = None) -> list[Finding]:
    """Findings for how the tests changed from one snapshot to another."""
    if from_ref == to_ref:
        return []
    before, after = changed_files(shadow, from_ref, to_ref, globs, listing)
    return audit_files(before, after, globs) if before or after else []


# --- Sessions ---------------------------------------------------------------------------

@dataclass
class SessionAudit:
    session_id: str
    base_ref: str | None  # the files the session started from (a fork: its fork point)
    end_ref: str | None
    findings: list[Finding]

    def weakened(self) -> bool:
        return any(f.severity != INFO and not f.intended for f in self.findings)

    def counts(self) -> dict[str, int]:
        """Findings by severity, leaving out those marked intended."""
        return {s: sum(f.severity == s and not f.intended for f in self.findings) for s in SEVERITIES} \
            | {"intended": sum(f.intended for f in self.findings)}

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "base_ref": self.base_ref, "end_ref": self.end_ref,
                "counts": self.counts(), "findings": [asdict(f) for f in self.findings]}


def session_range(store: Store, session: Session) -> tuple[str | None, str | None]:
    if session.parent_session:
        base = compare.turn_start_ref(store, session.parent_session, session.fork_turn)
    else:
        base = next((e.snapshot_ref for e in store.get_events(session.session_id) if e.snapshot_ref), None)
    return base, compare.end_ref(store, session)


def _attribute(shadow: ShadowRepo, events: list[Event], base: str, findings: list[Finding],
               globs: list[str], listing: dict) -> None:
    """Point each finding at the snapshot step that made it: a tool call, or between turns."""
    exact, by_path = {}, {}
    prev = base
    for e in events:
        if not e.snapshot_ref or e.snapshot_ref == prev:
            continue
        for f in audit_refs(shadow, prev, e.snapshot_ref, globs, listing):
            exact[(f.kind, f.path, f.test)] = by_path[f.path] = e  # the last change wins
        prev = e.snapshot_ref
    for f in findings:
        e = exact.get((f.kind, f.path, f.test)) or by_path.get(f.path)
        if e is not None:
            f.turn, f.seq = e.turn, e.seq
            f.tool = e.tool_name if e.kind == "tool_post" else None


def audit_tests(store: Store, shadow: ShadowRepo, sa: SessionAudit, command: str, globs: list[str],
                run: bool = False, rerun: bool = False) -> dict | None:
    """The session's final code tested with its own tests and with the tests it started with.

    Own tests passing while the original ones fail confirms the findings: the run only
    looks green because the tests changed. With run=False only cached results are used.
    """
    if not sa.base_ref or not sa.end_ref:
        return None

    def result(tests_ref: str) -> dict | None:
        cached = None if rerun else store.find_test_run(sa.end_ref, tests_ref, command)
        if cached is None and run:
            cached = compare.run_tests(store, shadow, sa.end_ref, tests_ref, command, globs)
        return cached and asdict(cached)

    changed = compare.test_files(shadow, sa.end_ref, globs) != compare.test_files(shadow, sa.base_ref, globs)
    own = result(sa.end_ref)
    original = result(sa.base_ref) if changed else own
    passed = lambda r: bool(r) and r["exit_code"] == 0
    return {"command": command, "tests_changed": changed, "own": own, "original": original,
            "gamed": passed(own) and original is not None and not passed(original)}


def audit_or_none(store: Store, shadow: ShadowRepo, session: Session, globs: list[str],
                  command: str | None = None) -> dict | None:
    """audit_session as a dict, or None if a snapshot can't be read: a page still renders.

    With a test command, a flagged session also gets its cached test results (`tests`)."""
    try:
        sa = audit_session(store, shadow, session, globs)
        d = sa.to_dict()
        if command and sa.findings:
            d["tests"] = audit_tests(store, shadow, sa, command, globs)
        return d
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None


def audit_session(store: Store, shadow: ShadowRepo, session: Session, globs: list[str]) -> SessionAudit:
    """How the session changed the tests, from its first snapshot (or fork point) to its last."""
    base, end = session_range(store, session)
    if not base or not end or base == end:
        return SessionAudit(session.session_id, base, end, [])
    key = f"{ANALYZER}:{','.join(globs)}"
    cached = store.find_audit(session.session_id, base, end, key)
    if cached is not None:
        findings = [Finding(**d) for d in cached]
    else:
        listing: dict = {}
        findings = audit_refs(shadow, base, end, globs, listing)
        if findings:
            _attribute(shadow, store.get_events(session.session_id), base, findings, globs, listing)
        store.add_audit(session.session_id, base, end, key, [asdict(f) for f in findings])
    reviews = store.find_reviews(session.session_id) if findings else {}
    for f in findings:
        f.intended = reviews.get(f.key) == "intended"
    return SessionAudit(session.session_id, base, end, findings)


# --- Git ranges -------------------------------------------------------------------------

MAX_COMMITS = 500


class AuditError(Exception):
    pass


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if result.returncode != 0:
        raise AuditError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def audit_git(root: Path, spec: str, globs: list[str]) -> tuple[str, str, int, list[Finding]]:
    """How the commits in `spec` changed the tests: "A..B" or "A...B" (both from their
    merge base, like a pull request), or "A" for A...HEAD. Each finding names its commit.

    Returns (base, end, number of commits, findings)."""
    a, sep, b = spec.partition("...") if "..." in spec else spec.partition("..")
    commit = lambda ref: _git(root, "rev-parse", "--verify", "--quiet", f"{ref or 'HEAD'}^{{commit}}")
    try:
        start, end = commit(a), commit(b if sep else "HEAD")
    except AuditError:
        raise AuditError(f"unknown revision in {spec!r}") from None
    try:
        base = _git(root, "merge-base", start, end)
    except AuditError:
        base = start
    repo = ShadowRepo(_git(root, "rev-parse", "--show-toplevel"), _git(root, "rev-parse", "--absolute-git-dir"))
    log = _git(root, "log", "--reverse", "--first-parent", "--format=%H %s", f"{base}..{end}")
    commits = [line.split(" ", 1) + [""] for line in log.splitlines()][-MAX_COMMITS:]
    listing: dict = {}
    findings = audit_refs(repo, base, end, globs, listing)
    if findings:
        steps = [Event("git", "tool_post", tool_name=f"{sha[:7]} {subject}".strip(), snapshot_ref=sha, seq=i)
                 for i, (sha, subject, *_) in enumerate(commits, 1)]
        _attribute(repo, steps, base, findings, globs, listing)
    return base, end, len(commits), findings
