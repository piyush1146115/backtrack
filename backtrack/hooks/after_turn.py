"""What the Stop hook does once a turn is recorded: check how the turn changed the tests.

If the turn weakened the tests (removed, skipped, fewer assertions...), the hook warns
the user with a systemMessage ("audit_warn": false in .backtrack/config.json turns this
off). With "audit_block": true it also stops the agent from finishing and tells it why,
once per turn. With "audit_run_tests": true, a session that weakened the tests has its
tests run in the background: its final code with its own tests and with the tests it
started with. The hook doesn't wait for them.
"""

from __future__ import annotations

import subprocess
import sys

from .. import audit, compare
from ..project import Project
from ..snapshot.shadow import ShadowRepo
from ..store.base import Store
from ..store.sqlite import SqliteStore

MAX_LISTED = 3


def run_tests_in_background(project: Project, session_id: str) -> None:
    with open(project.dir / "audit.log", "a") as log:
        subprocess.Popen([sys.executable, "-m", "backtrack.cli", "--dir", str(project.root),
                          "audit", session_id, "--run-tests"],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def turn_findings(store: Store, shadow: ShadowRepo, session_id: str, globs: list[str]) -> list[audit.Finding]:
    """How the last turn weakened the tests, from its first snapshot to its last."""
    events = store.get_events(session_id)
    start = next((e for e in reversed(events) if e.kind == "prompt"), None)
    if start is None or not start.snapshot_ref:
        return []
    refs = [e.snapshot_ref for e in events if e.seq > start.seq and e.snapshot_ref]
    if not refs:
        return []
    return [f for f in audit.audit_refs(shadow, start.snapshot_ref, refs[-1], globs) if f.severity != audit.INFO]


def warning(findings: list[audit.Finding], session_id: str) -> str:
    listed = [f"{f.path}::{f.test} {f.detail}" if f.test else f"{f.path} {f.detail}" for f in findings[:MAX_LISTED]]
    more = f" (+{len(findings) - MAX_LISTED} more)" if len(findings) > MAX_LISTED else ""
    return (f"Backtrack: this turn weakened the tests: {'; '.join(listed)}{more}. "
            f"Review with `backtrack audit {session_id[:8]}`.")


def after_turn(payload: dict, project: Project) -> dict | None:
    """Hook output for the end of a turn, if any."""
    home = Project(project.home) if project.is_fork else project
    config = compare.load_config(home)
    warn, block, run = config.get("audit_warn", True), config.get("audit_block", False), config.get("audit_run_tests", False)
    if not (warn or block or run):
        return None
    sid = payload["session_id"]
    shadow = ShadowRepo(home.root, home.git_dir)
    store = SqliteStore(project.db_path)
    try:
        findings = turn_findings(store, shadow, sid, config["test_globs"]) if warn or block else []
        session = next((s for s in store.list_sessions() if s.session_id == sid), None) if run else None
        weakened = session is not None and audit.audit_session(store, shadow, session, config["test_globs"]).weakened()
    finally:
        store.close()
    if weakened:
        run_tests_in_background(project, sid)
    if not findings:
        return None
    message = warning(findings, sid)
    if block and not payload.get("stop_hook_active"):  # once: the next stop of this turn goes through
        return {"decision": "block", "systemMessage": message,
                "reason": message + " Put back the tests you removed or weakened and fix the code instead, "
                                    "or if the user asked for this change, say so in your reply."}
    return {"systemMessage": message} if warn or block else None
