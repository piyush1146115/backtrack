"""JSON API over the event log, plus the static timeline page.

Some endpoints change files or start an agent, so every request must name this
server as its Host (which defeats DNS rebinding), and state-changing requests must be
same-origin JSON (which a cross-site page can't send without a CORS preflight).
"""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .. import audit, compare, rank
from ..fork import ForkError, create_fork_dir, fork_point, launch_fork, prepare_fork, restore_turn
from ..importer import import_transcript, transcript_dir
from ..install import recording_status, write_hook_settings
from ..overview import overview
from ..project import Project, find_project
from ..snapshot.shadow import ShadowRepo
from ..store.sqlite import SqliteStore

STATIC = Path(__file__).parent / "static"


class TestsRequest(BaseModel):
    a: str
    b: str
    rerun: bool = False
    test_command: str | None = None  # default: from the project config


class RewindRequest(BaseModel):
    session_id: str
    turn: int
    mode: Literal["restore", "copy"]


class UndoRequest(BaseModel):
    session_id: str
    snapshot: str


class ForkRequest(BaseModel):
    session_id: str
    turn: int
    instruction: str
    model: str | None = None
    allowed_tools: list[str] = []
    permission_mode: Literal["acceptEdits", "default", "plan", "bypassPermissions"] = "acceptEdits"


class Variant(BaseModel):
    instruction: str
    model: str | None = None


class RankRequest(BaseModel):
    session_id: str
    turn: int
    variants: list[Variant]
    allowed_tools: list[str] = []
    permission_mode: Literal["acceptEdits", "default", "plan", "bypassPermissions"] = "acceptEdits"


class RankTestsRequest(BaseModel):
    session_id: str
    turn: int
    rerun: bool = False
    test_command: str | None = None


class AuditTestsRequest(BaseModel):
    session_id: str
    rerun: bool = False


class AuditReviewRequest(BaseModel):
    session_id: str
    kind: str
    path: str
    test: str | None = None
    verdict: Literal["intended", "open"]


MAX_VARIANTS = 5


class ConfigRequest(BaseModel):
    test_command: str
    test_globs: list[str]


class RestoreSnapshotRequest(BaseModel):
    folder: str
    snapshot: str


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def create_app(project: Project, host: str = "127.0.0.1") -> FastAPI:
    app = FastAPI(title="Backtrack")
    shadow = ShadowRepo(project.root, project.git_dir)
    home = Project(project.home) if project.is_fork else project
    allowed_hosts = LOCAL_HOSTS | {host}

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        hostname = urlsplit("//" + request.headers.get("host", "")).hostname
        if hostname not in allowed_hosts:
            return JSONResponse({"detail": "unexpected Host header"}, status_code=403)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "cross-origin request"}, status_code=403)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"detail": "expected application/json"}, status_code=415)
        return await call_next(request)

    def open_store() -> SqliteStore:
        # One connection per request: endpoints run in a thread pool.
        return SqliteStore(project.db_path)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    def folders() -> list[Path]:
        """The project folder and its fork folders: the work trees snapshots can restore."""
        forks = sorted(p for p in home.forks_dir.iterdir() if (p / ".git").is_file()) \
            if home.forks_dir.is_dir() else []
        return [home.root, *forks]

    @app.get("/api/project")
    def get_project():
        config = compare.load_config(home)
        transcripts = transcript_dir(home.root)
        files = sorted(transcripts.glob("*.jsonl")) if transcripts.is_dir() else []
        store = open_store()
        try:
            known = {s.session_id for s in store.list_sessions()}
        finally:
            store.close()
        return {
            "root": str(project.root), "dir": str(home.dir),
            "test_command": config["test_command"], "test_globs": config["test_globs"],
            "hooks": recording_status(home.root),
            "hook_settings": str(home.root / ".claude" / "settings.local.json"),
            "transcripts": {"dir": str(transcripts), "files": len(files),
                            "not_recorded": sum(f.stem not in known for f in files)},
            "folders": [str(f) for f in folders()],
        }

    @app.post("/api/install")
    def install(_: dict | None = None):
        """Add (or repair) the recording hooks in the project's Claude Code settings."""
        return {"settings": str(write_hook_settings(home.root)), "hooks": recording_status(home.root)}

    @app.post("/api/import")
    def import_transcripts(_: dict | None = None):
        """Import the project's Claude Code transcripts; sessions already in the log are skipped."""
        transcripts = transcript_dir(home.root)
        paths = sorted(transcripts.glob("*.jsonl")) if transcripts.is_dir() else []
        store = open_store()
        try:
            imported = [sid for sid in (import_transcript(store, p) for p in paths) if sid]
        finally:
            store.close()
        return {"imported": imported, "skipped": len(paths) - len(imported)}

    @app.post("/api/config")
    def set_config(req: ConfigRequest):
        """Save the test command and test file patterns used by compare."""
        if not req.test_command.strip() or not any(g.strip() for g in req.test_globs):
            raise HTTPException(400, "give a test command and at least one test file pattern")
        path = home.dir / "config.json"
        config = json.loads(path.read_text()) if path.is_file() else {}
        config.update(test_command=req.test_command.strip(),
                      test_globs=[g.strip() for g in req.test_globs if g.strip()])
        path.write_text(json.dumps(config, indent=2) + "\n")
        return compare.load_config(home)

    def known_folder(folder: str) -> Project:
        path = Path(folder).resolve()
        if path not in [f.resolve() for f in folders()]:
            raise HTTPException(400, f"{folder} is not this project or one of its forks")
        return find_project(path)

    @app.get("/api/restore-points")
    def restore_points(folder: str):
        """States a folder was in just before each restore, newest first."""
        found = known_folder(folder)
        return ShadowRepo(found.root, found.git_dir).restore_points()

    @app.post("/api/restore-snapshot")
    def restore_snapshot(req: RestoreSnapshotRequest):
        """Put a folder back to a snapshot, e.g. to undo a rewind."""
        found = known_folder(req.folder)
        try:
            before = ShadowRepo(found.root, found.git_dir).restore(req.snapshot)
        except (ValueError, subprocess.CalledProcessError):
            raise HTTPException(400, f"no snapshot {req.snapshot}")
        return {"folder": str(found.root), "undo": before}

    @app.get("/api/sessions")
    def list_sessions():
        store = open_store()
        try:
            return [asdict(s) for s in store.list_sessions()]
        finally:
            store.close()

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str):
        store = open_store()
        try:
            session = next((s for s in store.list_sessions() if s.session_id == session_id), None)
            if session is None:
                raise HTTPException(404, f"no session {session_id}")
            events = []
            prev_ref = None
            for e in store.get_events(session_id):
                d = asdict(e)
                if e.snapshot_ref:
                    # What this snapshot is diffed against in the timeline.
                    d["prev_snapshot_ref"] = prev_ref
                    prev_ref = e.snapshot_ref
                events.append(d)
            config = compare.load_config(home)
            return {"session": asdict(session), "events": events,
                    "audit": audit.audit_or_none(store, shadow, session, config["test_globs"],
                                                 config["test_command"])}
        finally:
            store.close()

    @app.get("/api/audit")
    def get_audit():
        """How each session changed the tests: deleted, skipped, weakened."""
        config = compare.load_config(home)
        store = open_store()
        try:
            return [{"session": asdict(s), "audit": audit.audit_or_none(
                        store, shadow, s, config["test_globs"], config["test_command"])}
                    for s in store.list_sessions()]
        finally:
            store.close()

    @app.post("/api/audit/review")
    def review_finding(req: AuditReviewRequest):
        """Mark a finding as intended (or open it again). Returns the session's audit."""
        config = compare.load_config(home)
        store = open_store()
        try:
            session = next((s for s in store.list_sessions() if s.session_id == req.session_id), None)
            if session is None:
                raise HTTPException(404, f"no session {req.session_id}")
            key = audit.Finding(req.kind, "", req.path, "", test=req.test).key
            if key not in {f.key for f in audit.audit_session(store, shadow, session, config["test_globs"]).findings}:
                raise HTTPException(404, "no such finding in this session")
            store.add_review(req.session_id, key, req.verdict)
            return audit.audit_or_none(store, shadow, session, config["test_globs"], config["test_command"])
        finally:
            store.close()

    @app.post("/api/audit/tests")
    def run_audit_tests(req: AuditTestsRequest):
        """Run a session's final code with its own tests and with the tests it started with."""
        config = compare.load_config(home)
        store = open_store()
        try:
            session = next((s for s in store.list_sessions() if s.session_id == req.session_id), None)
            if session is None:
                raise HTTPException(404, f"no session {req.session_id}")
            sa = audit.audit_session(store, shadow, session, config["test_globs"])
            tests = audit.audit_tests(store, shadow, sa, config["test_command"], config["test_globs"],
                                      run=True, rerun=req.rerun)
            if tests is None:
                raise HTTPException(400, "this session has no workspace snapshots")
            return tests
        finally:
            store.close()

    @app.get("/api/search")
    def search(q: str, limit: int = Query(50, ge=1, le=500)):
        store = open_store()
        try:
            return [asdict(hit) for hit in store.search(q, limit=limit)]
        finally:
            store.close()

    @app.get("/api/irreversible")
    def irreversible():
        store = open_store()
        try:
            return [asdict(e) for e in store.irreversible_events()]
        finally:
            store.close()

    @app.get("/api/diff")
    def get_diff(from_ref: str = Query(alias="from"), to_ref: str = Query(alias="to")):
        try:
            return {"diff": shadow.diff(from_ref, to_ref)}
        except ValueError as e:
            raise HTTPException(400, str(e))
        except subprocess.CalledProcessError:
            raise HTTPException(404, "unknown snapshot")

    def comparison(store: SqliteStore, a: str, b: str) -> compare.Comparison:
        try:
            return compare.plan(store, shadow, a, b)
        except compare.CompareError as e:
            raise HTTPException(404, str(e))

    def test_matrix(store: SqliteStore, cmp: compare.Comparison, command: str | None = None,
                    **kwargs) -> dict:
        config = compare.load_config(home)
        command = (command or "").strip() or config["test_command"]
        return {"command": command,
                "rows": compare.test_matrix(store, shadow, cmp, command, config["test_globs"], **kwargs)}

    @app.get("/api/compare")
    def get_compare(a: str, b: str, test_command: str | None = None):
        store = open_store()
        try:
            cmp = comparison(store, a, b)
            return compare.to_dict(cmp, shadow) | {"tests": test_matrix(store, cmp, test_command)}
        finally:
            store.close()

    @app.post("/api/compare/tests")
    def run_compare_tests(req: TestsRequest):
        """Run the missing (or, with rerun, all) tests. Can take as long as the test suite."""
        store = open_store()
        try:
            cmp = comparison(store, req.a, req.b)
            return test_matrix(store, cmp, req.test_command, run=True, rerun=req.rerun)
        finally:
            store.close()

    def session_folder(store: SqliteStore, session_id: str) -> Project:
        session = next((s for s in store.list_sessions() if s.session_id == session_id), None)
        if session is None:
            raise HTTPException(404, f"no session {session_id}")
        found = find_project(session.cwd) if session.cwd and Path(session.cwd).is_dir() else None
        if found is None:
            raise HTTPException(400, f"the session's folder {session.cwd} no longer exists")
        return found

    @app.post("/api/rewind")
    def rewind(req: RewindRequest):
        """Files as at the start of a turn: restored in place, or copied to a new folder."""
        store = open_store()
        try:
            if req.mode == "restore":
                root, restored, before = restore_turn(store, req.session_id, req.turn)
                return {"folder": str(root), "restored": restored, "undo": before}
            point = fork_point(store, req.session_id, req.turn)
            return {"folder": str(create_fork_dir(home, point))}
        except ForkError as e:
            raise HTTPException(400, str(e))
        finally:
            store.close()

    @app.post("/api/undo")
    def undo(req: UndoRequest):
        """Restore a session's folder to a snapshot, e.g. the one taken before a rewind."""
        store = open_store()
        try:
            folder = session_folder(store, req.session_id)
            try:
                before = ShadowRepo(folder.root, folder.git_dir).restore(req.snapshot)
            except (ValueError, subprocess.CalledProcessError):
                raise HTTPException(400, f"no snapshot {req.snapshot}")
            return {"folder": str(folder.root), "undo": before}
        finally:
            store.close()

    def start_forks(session_id: str, turn: int, variants: list[Variant], permission_mode: str,
                    allowed_tools: list[str]) -> list[tuple[str, Path]]:
        """Prepare one fork per variant, then run them all in the background, in parallel."""
        store = open_store()
        try:
            point = fork_point(store, session_id, turn)
            prepared = [prepare_fork(store, home, point) for _ in variants]
        except ForkError as e:
            raise HTTPException(400, str(e))
        finally:
            store.close()

        def run(sid: str, root: Path, variant: Variant):
            fork_store = SqliteStore(project.db_path)  # connections can't cross threads
            try:
                launch_fork(fork_store, home, point, sid, root, variant.instruction,
                            model=(variant.model or "").strip() or None,
                            permission_mode=permission_mode, allowed_tools=allowed_tools)
            finally:
                fork_store.close()

        for (sid, root), variant in zip(prepared, variants):
            threading.Thread(target=run, args=(sid, root, variant), name=f"fork-{sid[:8]}",
                             daemon=True).start()
        return prepared

    @app.post("/api/fork")
    def fork(req: ForkRequest):
        """Start a fork in the background; its session fills in as the agent works."""
        if not req.instruction.strip():
            raise HTTPException(400, "the fork needs an instruction")
        [(session_id, fork_root)] = start_forks(
            req.session_id, req.turn, [Variant(instruction=req.instruction, model=req.model)],
            req.permission_mode, req.allowed_tools)
        return {"session_id": session_id, "folder": str(fork_root)}

    @app.post("/api/rank/forks")
    def rank_forks(req: RankRequest):
        """Fork one turn several ways at once, to rank the results."""
        variants = [v for v in req.variants if v.instruction.strip()]
        if not 1 <= len(variants) <= MAX_VARIANTS:
            raise HTTPException(400, f"give 1 to {MAX_VARIANTS} instructions")
        started = start_forks(req.session_id, req.turn, variants, req.permission_mode,
                              req.allowed_tools)
        return {"sessions": [sid for sid, _ in started]}

    def ranking(session_id: str, turn: int, command: str | None, **kwargs) -> dict:
        config = compare.load_config(home)
        command = (command or "").strip() or config["test_command"]
        store = open_store()
        try:
            base, found = rank.branches(store, shadow, session_id, turn)
            rank.add_tests(store, shadow, base, found, command, config["test_globs"], **kwargs)
        except rank.RankError as e:
            raise HTTPException(404, str(e))
        finally:
            store.close()
        return {"session_id": session_id, "turn": turn, "base_ref": base, "command": command,
                "branches": [rank.to_dict(b) for b in rank.ranked(found)]}

    @app.get("/api/rank")
    def get_rank(session_id: str, turn: int, test_command: str | None = None):
        """The original and its forks from one turn, best first (cached test results only)."""
        return ranking(session_id, turn, test_command)

    @app.post("/api/rank/tests")
    def run_rank_tests(req: RankTestsRequest):
        """Run the missing (or, with rerun, all) tests of the finished branches."""
        return ranking(req.session_id, req.turn, req.test_command, run=True, rerun=req.rerun)

    @app.get("/api/home")
    def get_home():
        config = compare.load_config(home)
        store = open_store()
        try:
            return overview(store, shadow, config["test_command"], config["test_globs"])
        finally:
            store.close()

    return app
