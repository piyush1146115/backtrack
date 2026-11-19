"""SQLite implementation of the Store interface.

Events are append-only: triggers reject UPDATE and DELETE. Sessions are written once.
seq and turn are assigned inside a BEGIN IMMEDIATE transaction, because Claude Code
runs hooks for parallel tool calls concurrently.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .base import KINDS, Event, SearchHit, Session, TestRun, now

# Searchable text of an event: tool name, every string in its input, and its output.
FTS_TEXT = """coalesce({e}.tool_name, '') || ' ' ||
    coalesce((SELECT group_concat(value, ' ') FROM json_tree({e}.input) WHERE type = 'text'), '')
    || ' ' || coalesce({e}.output, '')"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    cwd             TEXT,
    transcript_path TEXT,
    parent_session  TEXT,
    fork_turn       INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    session_id   TEXT NOT NULL REFERENCES sessions(session_id),
    seq          INTEGER NOT NULL,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ({", ".join(repr(k) for k in KINDS)})),
    turn         INTEGER NOT NULL,
    tool_name    TEXT,
    tool_use_id  TEXT,
    input        TEXT,
    output       TEXT,
    status       TEXT,
    snapshot_ref TEXT,
    irreversible INTEGER NOT NULL DEFAULT 0,
    tokens       TEXT,
    leaf_uuid    TEXT,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS test_runs (
    code_ref    TEXT NOT NULL,
    tests_ref   TEXT NOT NULL,
    command     TEXT NOT NULL,
    exit_code   INTEGER NOT NULL,
    summary     TEXT NOT NULL,
    output      TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS test_runs_key ON test_runs (code_ref, tests_ref, command);
CREATE TRIGGER IF NOT EXISTS test_runs_no_update BEFORE UPDATE ON test_runs
BEGIN SELECT RAISE(ABORT, 'test runs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS test_runs_no_delete BEFORE DELETE ON test_runs
BEGIN SELECT RAISE(ABORT, 'test runs are append-only'); END;
-- Test audits of sessions, a cache: derived from the snapshots, redone if missing.
CREATE TABLE IF NOT EXISTS audits (
    session_id TEXT NOT NULL,
    base_ref   TEXT NOT NULL,
    end_ref    TEXT NOT NULL,
    analyzer   TEXT NOT NULL,
    findings   TEXT NOT NULL,
    ts         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audits_key ON audits (session_id, base_ref, end_ref, analyzer);
CREATE TRIGGER IF NOT EXISTS audits_no_update BEFORE UPDATE ON audits
BEGIN SELECT RAISE(ABORT, 'audits are append-only'); END;
CREATE TRIGGER IF NOT EXISTS audits_no_delete BEFORE DELETE ON audits
BEGIN SELECT RAISE(ABORT, 'audits are append-only'); END;
-- A reviewer's verdict on an audit finding; the latest per finding counts.
CREATE TABLE IF NOT EXISTS audit_reviews (
    session_id TEXT NOT NULL,
    finding    TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    ts         TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_reviews_no_update BEFORE UPDATE ON audit_reviews
BEGIN SELECT RAISE(ABORT, 'audit reviews are append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_reviews_no_delete BEFORE DELETE ON audit_reviews
BEGIN SELECT RAISE(ABORT, 'audit reviews are append-only'); END;
-- Full-text index over events: a derived index, rebuilt from the log if missing.
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    text, session_id UNINDEXED, seq UNINDEXED, tokenize = 'porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS events_fts_insert AFTER INSERT ON events
BEGIN
    INSERT INTO events_fts (text, session_id, seq) VALUES ({FTS_TEXT.format(e="new")}, new.session_id, new.seq);
END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS sessions_no_update BEFORE UPDATE ON sessions
BEGIN SELECT RAISE(ABORT, 'sessions are write-once'); END;
CREATE TRIGGER IF NOT EXISTS sessions_no_delete BEFORE DELETE ON sessions
BEGIN SELECT RAISE(ABORT, 'sessions are write-once'); END;
"""

EVENT_COLUMNS = (
    "session_id", "seq", "ts", "kind", "turn", "tool_name", "tool_use_id", "input",
    "output", "status", "snapshot_ref", "irreversible", "tokens", "leaf_uuid",
)


class SqliteStore:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Autocommit mode; transactions are explicit. timeout sets busy_timeout.
        self.conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        has_fts = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'events_fts'").fetchone()
        self.conn.executescript(SCHEMA)
        if not has_fts:  # a database from before search: index the existing events
            self.conn.execute(f"INSERT INTO events_fts (text, session_id, seq)"
                              f" SELECT {FTS_TEXT.format(e='e')}, session_id, seq FROM events e")

    def close(self) -> None:
        self.conn.close()

    def ensure_session(self, session: Session) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
            (session.session_id, session.started_at, session.cwd, session.transcript_path,
             session.parent_session, session.fork_turn),
        )

    def append_event(self, event: Event) -> Event:
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            seq, turn = c.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1, COALESCE(MAX(turn), 0)"
                " FROM events WHERE session_id = ?",
                (event.session_id,),
            ).fetchone()
            event.seq = seq
            event.turn = turn + 1 if event.kind == "prompt" else turn
            c.execute(
                f"INSERT INTO events ({', '.join(EVENT_COLUMNS)})"
                f" VALUES ({', '.join('?' * len(EVENT_COLUMNS))})",
                (event.session_id, event.seq, event.ts, event.kind, event.turn,
                 event.tool_name, event.tool_use_id, _dumps(event.input), event.output,
                 event.status, event.snapshot_ref, int(event.irreversible),
                 _dumps(event.tokens), event.leaf_uuid),
            )
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        return event

    def list_sessions(self) -> list[Session]:
        rows = self.conn.execute("""
            SELECT s.*,
                   MAX(e.ts) AS last_ts,
                   COALESCE(MAX(e.turn), 0) AS turns,
                   COUNT(e.seq) AS events,
                   COALESCE(SUM(json_extract(e.tokens, '$.output')), 0) AS output_tokens,
                   COALESCE(SUM(e.kind = 'tool_pre' AND e.irreversible), 0) AS irreversible,
                   (SELECT json_extract(p.input, '$.prompt') FROM events p
                     WHERE p.session_id = s.session_id AND p.kind = 'prompt'
                     ORDER BY p.seq LIMIT 1) AS first_prompt
              FROM sessions s LEFT JOIN events e ON e.session_id = s.session_id
             GROUP BY s.session_id
             ORDER BY COALESCE(MAX(e.ts), s.started_at) DESC
        """).fetchall()
        return [Session(**dict(r)) for r in rows]

    def get_events(self, session_id: str, upto_seq: int | None = None) -> list[Event]:
        sql = "SELECT * FROM events WHERE session_id = ?"
        params: list = [session_id]
        if upto_seq is not None:
            sql += " AND seq <= ?"
            params.append(upto_seq)
        rows = self.conn.execute(sql + " ORDER BY seq", params).fetchall()
        return [_row_to_event(r) for r in rows]

    def search(self, query: str, limit: int = 50) -> list[SearchHit]:
        match = fts_query(query)
        if not match:
            return []
        rows = self.conn.execute("""
            SELECT e.session_id, e.seq, e.turn, e.kind, e.tool_name, e.ts, e.irreversible,
                   snippet(events_fts, 0, char(1), char(2), '…', 16) AS snippet
              FROM events_fts f JOIN events e ON e.session_id = f.session_id AND e.seq = f.seq
             WHERE events_fts MATCH ?
             ORDER BY f.rank LIMIT ?
        """, (match, limit)).fetchall()
        return [SearchHit(**{**dict(r), "irreversible": bool(r["irreversible"])}) for r in rows]

    def irreversible_events(self) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE kind = 'tool_pre' AND irreversible ORDER BY ts DESC").fetchall()
        return [_row_to_event(r) for r in rows]

    def add_test_run(self, run: TestRun) -> None:
        self.conn.execute(
            "INSERT INTO test_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run.code_ref, run.tests_ref, run.command, run.exit_code, run.summary,
             run.output, run.duration_ms, run.ts),
        )

    def find_test_run(self, code_ref: str, tests_ref: str, command: str) -> TestRun | None:
        row = self.conn.execute(
            "SELECT * FROM test_runs WHERE code_ref = ? AND tests_ref = ? AND command = ?"
            " ORDER BY ts DESC LIMIT 1",
            (code_ref, tests_ref, command),
        ).fetchone()
        return TestRun(**dict(row)) if row else None

    def add_audit(self, session_id: str, base_ref: str, end_ref: str, analyzer: str,
                  findings: list[dict]) -> None:
        self.conn.execute("INSERT INTO audits VALUES (?, ?, ?, ?, ?, ?)",
                          (session_id, base_ref, end_ref, analyzer, json.dumps(findings), now()))

    def find_audit(self, session_id: str, base_ref: str, end_ref: str, analyzer: str) -> list[dict] | None:
        row = self.conn.execute(
            "SELECT findings FROM audits WHERE session_id = ? AND base_ref = ? AND end_ref = ?"
            " AND analyzer = ? ORDER BY ts DESC LIMIT 1",
            (session_id, base_ref, end_ref, analyzer),
        ).fetchone()
        return json.loads(row["findings"]) if row else None

    def add_review(self, session_id: str, finding: str, verdict: str) -> None:
        self.conn.execute("INSERT INTO audit_reviews VALUES (?, ?, ?, ?)", (session_id, finding, verdict, now()))

    def find_reviews(self, session_id: str) -> dict[str, str]:
        rows = self.conn.execute("SELECT finding, verdict FROM audit_reviews WHERE session_id = ? ORDER BY rowid",
                                 (session_id,)).fetchall()
        return {r["finding"]: r["verdict"] for r in rows}


def fts_query(text: str) -> str:
    """Turn free text into an FTS5 query: every word must match, as a prefix.

    Each word is quoted, so FTS5 syntax characters in the input are taken literally.
    """
    words = text.split()
    return " ".join('"' + w.replace('"', '""') + '"*' for w in words)


def _dumps(value) -> str | None:
    return None if value is None else json.dumps(value)


def _row_to_event(row: sqlite3.Row) -> Event:
    d = dict(row)
    d["input"] = json.loads(d["input"]) if d["input"] is not None else None
    d["tokens"] = json.loads(d["tokens"]) if d["tokens"] is not None else None
    d["irreversible"] = bool(d["irreversible"])
    return Event(**d)
