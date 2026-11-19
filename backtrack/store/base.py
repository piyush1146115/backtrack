"""Storage interface. The SQLite implementation can be swapped for another engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

KINDS = ("session_start", "prompt", "tool_pre", "tool_post", "stop", "error", "session_end")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Event:
    session_id: str
    kind: str
    ts: str = field(default_factory=now)
    tool_name: str | None = None
    tool_use_id: str | None = None
    input: Any = None  # JSON-serialisable
    output: str | None = None  # truncated summary
    status: str | None = None  # tool_post: ok | failed | interrupted
    snapshot_ref: str | None = None
    irreversible: bool = False
    tokens: dict | None = None
    leaf_uuid: str | None = None  # stop: last transcript message uuid of the turn
    # Assigned by the store on append.
    seq: int | None = None
    turn: int | None = None


@dataclass
class Session:
    session_id: str
    started_at: str
    cwd: str | None = None
    transcript_path: str | None = None
    parent_session: str | None = None
    fork_turn: int | None = None
    # Aggregates filled in by list_sessions.
    last_ts: str | None = None
    turns: int = 0
    events: int = 0
    output_tokens: int = 0
    first_prompt: str | None = None
    irreversible: int = 0  # points of no return


@dataclass
class SearchHit:
    session_id: str
    seq: int
    turn: int
    kind: str
    tool_name: str | None
    ts: str
    irreversible: bool
    snippet: str  # matches are wrapped in \x01 … \x02


@dataclass
class TestRun:
    """Result of running a test command on one snapshot's code with one snapshot's tests."""
    __test__ = False  # not a pytest test class

    code_ref: str
    tests_ref: str
    command: str
    exit_code: int
    summary: str  # e.g. "3 failed, 3 passed"
    output: str  # truncated
    duration_ms: int
    ts: str = field(default_factory=now)


class Store(Protocol):
    def ensure_session(self, session: Session) -> None:
        """Record a session the first time it is seen. Never modifies an existing one."""

    def append_event(self, event: Event) -> Event:
        """Append an event, assigning its per-session seq and turn. Returns the stored event."""

    def list_sessions(self) -> list[Session]:
        """All sessions, most recent first."""

    def get_events(self, session_id: str, upto_seq: int | None = None) -> list[Event]:
        """Events of a session in seq order, optionally only those with seq <= upto_seq."""

    def search(self, query: str, limit: int = 50) -> list[SearchHit]:
        """Events matching every word of `query` (as prefixes), best matches first."""

    def irreversible_events(self) -> list[Event]:
        """All points of no return across sessions, most recent first."""

    def add_test_run(self, run: TestRun) -> None:
        """Record a test run. Runs are append-only like events."""

    def find_test_run(self, code_ref: str, tests_ref: str, command: str) -> TestRun | None:
        """The most recent run for this code, tests and command, if any."""

    def add_audit(self, session_id: str, base_ref: str, end_ref: str, analyzer: str,
                  findings: list[dict]) -> None:
        """Record a session's test audit between two snapshots. Append-only."""

    def find_audit(self, session_id: str, base_ref: str, end_ref: str, analyzer: str) -> list[dict] | None:
        """The findings of the most recent matching audit, if any."""

    def add_review(self, session_id: str, finding: str, verdict: str) -> None:
        """Record a reviewer's verdict on a finding ("intended" or "open"). Append-only."""

    def find_reviews(self, session_id: str) -> dict[str, str]:
        """{finding key: latest verdict} for a session."""
