"""backtrack install | import | sessions | show | search | rewind | undo | fork | compare | audit |
serve | open | doctor | pause | resume"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path

from . import audit, compare
from .fork import ForkError, ForkPoint, create_fork_dir, fork_point, restore_turn, run_fork
from .importer import import_transcript, transcript_dir
from .install import (PAUSED_FILE, auto_root, exclude_backtrack, hook_status, init_project,
                      plugin_enabled, recording_status, write_hook_settings)
from .project import Project, default_root, find_project
from .snapshot.shadow import ShadowRepo
from .store.base import Event, Session
from .store.sqlite import SqliteStore

def cmd_install(args) -> int:
    root = Path(args.dir) if args.dir else default_root(os.getcwd())
    project = init_project(root)
    settings_path = write_hook_settings(project.root)
    print(f"Backtrack installed in {project.root}")
    print(f"  hooks:  {settings_path}")
    print(f"  data:   {project.dir}")
    print("Start a new Claude Code session in that folder to record it.")
    return 0


def open_store(args) -> tuple[Project, SqliteStore]:
    start = args.dir or os.getcwd()
    project = find_project(start)
    if project is None:
        sys.exit(f"No Backtrack project found at or above {start}. Run `backtrack install` first.")
    return project, SqliteStore(project.db_path)


def cmd_import(args) -> int:
    project, store = open_store(args)
    paths = [Path(p) for p in args.transcripts] or \
        sorted(transcript_dir(project.root).glob("*.jsonl"))
    if not paths:
        print(f"No transcripts found in {transcript_dir(project.root)}.")
        return 0
    imported = 0
    for path in paths:
        sid = import_transcript(store, path)
        if sid:
            imported += 1
            print(f"imported {sid[:8]}  {path.name}")
    print(f"{imported} imported, {len(paths) - imported} skipped (empty or already recorded).")
    return 0


def strip_fork_note(prompt: str | None) -> str:
    """Drop the note Backtrack prepends to a fork's first prompt."""
    if prompt and prompt.startswith("[Backtrack] ") and "\n\n" in prompt:
        return prompt.split("\n\n", 1)[1]
    return prompt or ""


def short(text: str | None, n: int = 70) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def cmd_sessions(args) -> int:
    _, store = open_store(args)
    sessions = store.list_sessions()
    if not sessions:
        print("No sessions recorded yet.")
        return 0
    print(f"{'SESSION':10} {'LAST ACTIVITY':20} {'TURNS':>5} {'EVENTS':>6} {'OUT TOK':>7}  FIRST PROMPT")
    for s in sessions:
        when = (s.last_ts or s.started_at)[:19].replace("T", " ")
        fork = f"[fork of {s.parent_session[:8]} @ turn {s.fork_turn}] " if s.parent_session else ""
        print(f"{s.session_id[:8]:10} {when:20} {s.turns:>5} {s.events:>6} "
              f"{s.output_tokens:>7}  {fork}{short(strip_fork_note(s.first_prompt), 60)}")
    return 0


def resolve_session(store: SqliteStore, prefix: str) -> Session:
    matches = [s for s in store.list_sessions() if s.session_id.startswith(prefix)]
    if not matches:
        sys.exit(f"No session matching {prefix!r}.")
    if len(matches) > 1:
        sys.exit(f"{prefix!r} matches {len(matches)} sessions; use a longer prefix.")
    return matches[0]


def describe_input(tool_input, cwd: str | None) -> str:
    if not isinstance(tool_input, dict):
        return short(json.dumps(tool_input))
    for key in ("command", "file_path", "notebook_path", "pattern", "url", "query", "prompt"):
        if key in tool_input:
            value = str(tool_input[key])
            if cwd and key.endswith("path") and value.startswith(cwd.rstrip("/") + "/"):
                value = value[len(cwd.rstrip("/")) + 1:]
            return short(value)
    return short(json.dumps(tool_input))


def describe(e: Event, cwd: str | None) -> str:
    if e.kind == "session_start":
        return f"session start ({(e.input or {}).get('source')})"
    if e.kind == "prompt":
        return f'> "{short(strip_fork_note((e.input or {}).get("prompt")), 80)}"'
    if e.kind == "tool_pre":
        return f"→ {e.tool_name}: {describe_input(e.input, cwd)}"
    if e.kind == "tool_post":
        mark = "←" if e.status == "ok" else "✗"
        return f"{mark} {e.tool_name} {e.status}: {short(e.output, 60)}"
    if e.kind == "stop":
        t = e.tokens or {}
        tokens = f" [tokens in {t.get('input', 0) + t.get('cache_read', 0) + t.get('cache_creation', 0)}" \
                 f" / out {t.get('output', 0)}]" if t else ""
        return f"■ stop: {short(e.output, 60)}{tokens}"
    if e.kind == "error":
        return f"! error: {short(json.dumps(e.input), 80)}"
    if e.kind == "session_end":
        return f"session end ({(e.input or {}).get('reason')})"
    return e.kind


def cmd_show(args) -> int:
    _, store = open_store(args)
    session = resolve_session(store, args.session)
    print(f"Session {session.session_id}")
    print(f"  cwd:        {session.cwd}")
    print(f"  started:    {session.started_at}")
    print(f"  transcript: {session.transcript_path}")
    if session.parent_session:
        print(f"  forked from {session.parent_session} at turn {session.fork_turn}")
    offset = session.fork_turn - 1 if session.fork_turn else 0
    turn = None
    for e in store.get_events(session.session_id):
        if e.turn != turn:
            turn = e.turn
            print(f"\nTurn {turn + offset}" if turn else "\nSetup")
        flags = ""
        if e.snapshot_ref:
            flags += f"  [snap {e.snapshot_ref[:7]}]"
        if e.irreversible:
            flags += "  ⚠ POINT OF NO RETURN"
        print(f"  #{e.seq:<4} {e.ts[11:19]}  {describe(e, session.cwd)}{flags}")
    return 0


def display_turn(session: Session | None, turn: int) -> int:
    """A fork's turns are numbered on from its fork point, as in the original."""
    return turn + session.fork_turn - 1 if session and session.fork_turn else turn


def cmd_search(args) -> int:
    _, store = open_store(args)
    sessions = {s.session_id: s for s in store.list_sessions()}
    if args.irreversible:
        events = store.irreversible_events()
        for e in events:
            s = sessions.get(e.session_id)
            print(f"{e.session_id[:8]}  turn {display_turn(s, e.turn):<3} #{e.seq:<4} {e.ts[:19].replace('T', ' ')}"
                  f"  ⚠ {e.tool_name}: {describe_input(e.input, s and s.cwd)}")
        print(f"{len(events)} point(s) of no return.")
        return 0
    if not " ".join(args.query).strip():
        sys.exit("Give a search query, or --irreversible to list points of no return.")
    hits = store.search(" ".join(args.query), limit=args.limit)
    on, off = ("\033[1m", "\033[0m") if sys.stdout.isatty() else ("[", "]")
    for hit in hits:
        snippet = " ".join(hit.snippet.split()).replace("\x01", on).replace("\x02", off)
        what = f"{hit.kind} {hit.tool_name}" if hit.tool_name else hit.kind
        mark = "  ⚠" if hit.irreversible else ""
        print(f"{hit.session_id[:8]}  turn {display_turn(sessions.get(hit.session_id), hit.turn):<3}"
              f" #{hit.seq:<4} {what:20} {snippet}{mark}")
    print(f"{len(hits)} match(es).")
    return 0


def home_project(project: Project) -> Project:
    """The project that owns the shadow repo (a fork's parent)."""
    return Project(project.home) if project.is_fork else project


def load_fork_point(store: SqliteStore, args) -> ForkPoint:
    session = resolve_session(store, args.session)
    try:
        return fork_point(store, session.session_id, args.turn)
    except ForkError as e:
        sys.exit(str(e))


def print_irreversible(point: ForkPoint) -> None:
    if not point.irreversible:
        return
    print(f"\n⚠ Points of no return from turn {point.turn} on. Files and conversation are "
          "restored, but these outside effects already happened:")
    for e in point.irreversible:
        print(f"   turn {e.turn} #{e.seq}  {e.tool_name}: {describe_input(e.input, point.session.cwd)}")


def cmd_rewind(args) -> int:
    project, store = open_store(args)
    point = load_fork_point(store, args)
    if args.in_place:
        try:
            root, _, before = restore_turn(store, point.session.session_id, point.turn)
        except ForkError as e:
            sys.exit(str(e))
        print(f"Restored {root} to the start of turn {point.turn}.")
        print(f"Your files from before are in snapshot {before[:7]}. To undo:\n"
              f"  backtrack --dir {shlex.quote(str(root))} undo {before[:12]}")
        print_irreversible(point)
        return 0
    fork_root = create_fork_dir(home_project(project), point)
    sid = point.session.session_id
    print(f"Files as they were at the start of turn {point.turn} of {sid[:8]}:\n  {fork_root}")
    print_irreversible(point)
    print(f"\nTo redo turn {point.turn} with a new instruction:\n"
          f"  backtrack fork {sid[:8]} {point.turn} \"<instruction>\"")
    return 0


def cmd_undo(args) -> int:
    """Put the project folder back to a snapshot, e.g. the one taken before a rewind."""
    project, _ = open_store(args)
    try:
        before = ShadowRepo(project.root, project.git_dir).restore(args.snapshot)
    except (ValueError, subprocess.CalledProcessError):
        sys.exit(f"No snapshot {args.snapshot!r}.")
    print(f"Restored {project.root} to snapshot {args.snapshot[:7]}.")
    print(f"The files from just before are in snapshot {before[:7]}.")
    return 0


def cmd_fork(args) -> int:
    project, store = open_store(args)
    point = load_fork_point(store, args)
    sid = point.session.session_id
    print_irreversible(point)
    print(f"Forking {sid[:8]} at turn {point.turn}: running Claude Code in a new folder "
          "(this can take a few minutes)…")
    result = run_fork(store, home_project(project), point, args.instruction, model=args.model,
                      permission_mode=args.permission_mode, allowed_tools=args.allow)
    print(f"\nfork session: {result.session_id}\nfolder:       {result.fork_root}")
    print(f"\n{'Claude Code reported an error' if result.is_error else 'Result'}:\n{result.result}")
    print(f"\nContinue interactively:\n  cd {shlex.quote(str(result.fork_root))} && "
          f"claude --resume {result.session_id}")
    return 1 if result.is_error else 0


def session_label(s: Session) -> str:
    fork = f" (fork of {s.parent_session[:8]} @ turn {s.fork_turn})" if s.parent_session else ""
    return f"{s.session_id[:8]}{fork}"


def cmd_compare(args) -> int:
    project, store = open_store(args)
    home = home_project(project)
    shadow = ShadowRepo(home.root, home.git_dir)
    a, b = resolve_session(store, args.a), resolve_session(store, args.b)
    try:
        cmp = compare.plan(store, shadow, a.session_id, b.session_id)
    except compare.CompareError as e:
        sys.exit(str(e))
    print(f"A  {session_label(a)}  {short(strip_fork_note(a.first_prompt), 50)}")
    print(f"B  {session_label(b)}  {short(strip_fork_note(b.first_prompt), 50)}")
    print(f"Base: {cmp.base_label} [snap {cmp.base_ref[:7]}]")
    for name, side in (("A", cmp.a), ("B", cmp.b)):
        files = shadow.diff_stat(cmp.base_ref, side.ref)
        print(f"\n{name} changed {len(files)} file(s) since the base [snap {side.ref[:7]}]")
        for f in files:
            counts = "binary" if f["added"] is None else f"+{f['added']} -{f['deleted']}"
            print(f"   {f['path']:50} {counts}")
    if args.no_tests:
        return 0
    config = compare.load_config(home)
    command = args.test_cmd or config["test_command"]
    print(f"\nRunning tests: {command}")
    rows = compare.test_matrix(store, shadow, cmp, command, config["test_globs"],
                               run=True, rerun=args.rerun)
    print(f"\n   {'':12} {'OWN TESTS':32} ORIGINAL TESTS (as at the base)")
    for row in rows:
        own, original = row["own"]["summary"], row["original"]["summary"]
        if row["tests_changed"]:
            own += " (tests changed)"
        print(f"   {row['side'].upper() if row['side'] != 'base' else 'base':12} {own:32} {original}")
    return 0


def finding_line(f: audit.Finding, n: int | None = None) -> str:
    where = f"turn {f.turn}, {f.tool or 'between turns'}" if f.turn else (f.tool or "")  # git: the commit
    name = f"{f.path}::{f.test}" if f.test else f.path
    number = f"{n:>3}. " if n is not None else "   "
    return (f"{number}{f.severity.upper():7} {name:48} {f.detail}" + (f"  ({where})" if where else "")
            + ("  [marked intended]" if f.intended else ""))


def tests_line(t: dict) -> str:
    summary = lambda run: run["summary"] if run else "not run"
    line = f"   Own tests: {summary(t['own'])}"
    if t["tests_changed"]:
        line += f" · tests as the session started: {summary(t['original'])}"
    if t["gamed"]:
        line += "  → only green because it changed the tests"
    return line


def audit_git_range(args) -> int:
    if args.session or args.run_tests or args.intended or args.reopen:
        sys.exit("--git audits commits: it takes no session, --run-tests, --intended or --reopen.")
    root = default_root(args.dir or os.getcwd())
    project = find_project(root)
    globs = compare.load_config(home_project(project))["test_globs"] if project else compare.DEFAULT_TEST_GLOBS
    try:
        base, end, commits, findings = audit.audit_git(root, args.git, globs)
    except audit.AuditError as e:
        sys.exit(str(e))
    if args.json:
        print(json.dumps({"range": args.git, "base_ref": base, "end_ref": end, "commits": commits,
                          "findings": [asdict(f) for f in findings]}, indent=2))
    elif not findings:
        print(f"The tests were not weakened in {args.git} ({plural(commits, 'commit')}, {base[:7]} → {end[:7]}).")
    else:
        print(f"Test changes in {args.git} ({plural(commits, 'commit')}, {base[:7]} → {end[:7]}):")
        for n, f in enumerate(findings, 1):
            print(finding_line(f, n))
    if args.fail_on:
        worst = audit.SEVERITIES[: audit.SEVERITIES.index(args.fail_on) + 1]
        return 1 if any(f.severity in worst for f in findings) else 0
    return 0


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def cmd_audit(args) -> int:
    if args.git:
        return audit_git_range(args)
    project, store = open_store(args)
    home = home_project(project)
    shadow = ShadowRepo(home.root, home.git_dir)
    config = compare.load_config(home)
    globs, command = config["test_globs"], args.test_cmd or config["test_command"]
    sessions = [resolve_session(store, args.session)] if args.session else store.list_sessions()
    audits = [(s, audit.audit_session(store, shadow, s, globs)) for s in sessions]
    if args.intended or args.reopen:
        if not args.session:
            sys.exit("--intended and --reopen need a session.")
        s, a = audits[0]
        for verdict, numbers in (("intended", args.intended), ("open", args.reopen)):
            for n in numbers:
                if not 1 <= n <= len(a.findings):
                    sys.exit(f"No finding {n}: the session has {len(a.findings)}.")
                store.add_review(s.session_id, a.findings[n - 1].key, verdict)
        audits = [(s, audit.audit_session(store, shadow, s, globs))]
    tests = {}
    if args.run_tests:
        for s, a in audits:
            if a.findings or args.session:
                if not args.json:
                    print(f"Running tests for {s.session_id[:8]}: {command}", file=sys.stderr)
                tests[s.session_id] = audit.audit_tests(store, shadow, a, command, globs,
                                                        run=True, rerun=args.rerun)
    if args.json:
        print(json.dumps([a.to_dict() | ({"tests": tests[s.session_id]} if s.session_id in tests else {})
                          for s, a in audits], indent=2))
    elif args.session:
        s, a = audits[0]
        print(f"{session_label(s)}  {short(strip_fork_note(s.first_prompt), 60)}")
        if a.base_ref is None or a.end_ref is None:
            print("No workspace snapshots: nothing to audit.")
        elif not a.findings:
            print(f"The tests were not weakened [snap {a.base_ref[:7]} → {a.end_ref[:7]}].")
        else:
            print(f"Test changes [snap {a.base_ref[:7]} → {a.end_ref[:7]}]:")
            for n, f in enumerate(a.findings, 1):
                print(finding_line(f, n))
        if tests.get(s.session_id):
            print(tests_line(tests[s.session_id]))
    else:
        flagged = [(s, a) for s, a in audits if any(not f.intended for f in a.findings)]
        if not flagged:
            print(f"No session weakened the tests ({len(audits)} audited).")
        else:
            print(f"{'SESSION':10} {'HIGH':>4} {'MED':>4} {'INFO':>4}  FIRST PROMPT")
            for s, a in flagged:
                c = a.counts()
                print(f"{s.session_id[:8]:10} {c['high']:>4} {c['medium']:>4} {c['info']:>4}  "
                      f"{short(strip_fork_note(s.first_prompt), 60)}")
            for s, a in flagged:
                if tests.get(s.session_id):
                    print(f"{s.session_id[:8]}  {tests_line(tests[s.session_id]).strip()}")
            print(f"\n{len(flagged)} of {len(audits)} sessions changed the tests. "
                  "`backtrack audit <session>` shows what changed.")
    if args.fail_on:
        worst = audit.SEVERITIES[: audit.SEVERITIES.index(args.fail_on) + 1]
        return 1 if any(f.severity in worst and not f.intended for _, a in audits for f in a.findings) else 0
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .server.app import create_app

    project, store = open_store(args)
    store.close()
    print(f"Backtrack timeline: http://{args.host}:{args.port}")
    uvicorn.run(create_app(project, host=args.host), host=args.host, port=args.port, log_level="warning")
    return 0


def served_root(url: str) -> str | None:
    """The project a Backtrack server at `url` shows; None if nothing listens there,
    "" if something else does."""
    try:
        with urllib.request.urlopen(url + "/api/project", timeout=1) as r:
            return json.load(r).get("root", "")
    except urllib.error.URLError as e:
        return None if isinstance(e.reason, ConnectionRefusedError) else ""
    except (OSError, ValueError):
        return ""


def cmd_open(args) -> int:
    """Open the timeline in a browser, starting a server in the background if needed."""
    import webbrowser

    project, store = open_store(args)
    store.close()
    for port in range(args.port, args.port + 10):
        url = f"http://127.0.0.1:{port}"
        root = served_root(url)
        if root == str(project.root):
            break
        if root is None:
            log = open(project.dir / "server.log", "a")
            subprocess.Popen([sys.executable, "-c", "import sys; from backtrack.cli import main; "
                              "sys.exit(main(sys.argv[1:]))", "--dir", str(project.root),
                              "serve", "--port", str(port)],
                             stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
            for _ in range(50):
                time.sleep(0.2)
                if served_root(url) == str(project.root):
                    break
            else:
                sys.exit(f"The server didn't start; see {project.dir / 'server.log'}")
            break
    else:
        sys.exit(f"Ports {args.port}-{args.port + 9} are all in use.")
    print(f"Backtrack timeline for {project.root}: {url}")
    if not args.no_browser:
        webbrowser.open(url)
    return 0


def cmd_doctor(args) -> int:
    """Check that Backtrack can record the current project, and say how to fix it if not."""
    from importlib.metadata import version

    cwd = args.dir or os.getcwd()

    def check(ok: bool | None, text: str) -> None:
        print({True: "✓", False: "✗", None: "-"}[ok], text)

    print(f"Backtrack {version('backtrack')}")
    check(bool(shutil.which("git")), "git" if shutil.which("git") else "git not found on PATH")
    claude = shutil.which("claude")
    claude_version = subprocess.run([claude, "--version"], capture_output=True, text=True).stdout.strip() \
        if claude else ""
    check(bool(claude), claude_version or "Claude Code not found on PATH (needed to record and fork)")
    plugin = plugin_enabled()
    hook = shutil.which("backtrack-hook") or (Path.home() / ".local/bin/backtrack-hook"
                                               if (Path.home() / ".local/bin/backtrack-hook").exists() else None)
    if plugin:
        check(bool(hook), f"plugin enabled, recording with {hook}" if hook else
              "plugin enabled, but `backtrack-hook` is not on PATH: run `uv tool update-shell` "
              "and restart Claude Code")
    else:
        check(None, "plugin not installed: only folders set up with `backtrack install` are recorded")

    project = find_project(cwd)
    root = project.home or project.root if project else default_root(cwd)
    status = recording_status(root)
    reason = auto_root(root)[1] if plugin and status == "missing" else ""
    recording = status == "installed" or (status == "plugin" and bool(hook))
    print(f"\nProject {root}")
    check(recording, {
        "installed": "recording: this project's hooks",
        "plugin": "recording: the Backtrack plugin",
        "paused": "paused: run `backtrack resume` to record again",
        "outdated": "the project's hooks run another Backtrack: run `backtrack install` again",
        "missing": f"not recording ({reason}); run `backtrack install`" if reason
                   else "not recording: install the plugin, or run `backtrack install` here",
    }[status])
    if project and project.db_path.exists():
        store = SqliteStore(project.db_path)
        try:
            sessions = store.list_sessions()
        finally:
            store.close()
        last = max((s.last_ts or s.started_at for s in sessions), default=None)
        check(None, f"{len(sessions)} session{'' if len(sessions) == 1 else 's'} recorded" + (f", last activity {last[:16].replace('T', ' ')}" if last else ""))
    log = project.error_log if project else None
    if log and log.is_file() and log.stat().st_size:
        entries = log.read_text().split("--- ")[1:]
        last_line = [l for l in entries[-1].splitlines() if l.strip() and not l.startswith("payload:")][-1]
        check(False, f"{len(entries)} hook errors in {log}; the last: {last_line.strip()}")
    return 0 if recording else 1


def cmd_pause(args) -> int:
    root = Path(args.dir) if args.dir else default_root(os.getcwd())
    found = find_project(root)
    folder = found.dir if found else root / ".backtrack"
    folder.mkdir(exist_ok=True)
    exclude_backtrack(found.home or found.root if found else root)
    (folder / PAUSED_FILE).write_text("")
    print(f"Recording paused for {folder.parent}. `backtrack resume` starts it again.")
    return 0


def cmd_resume(args) -> int:
    root = Path(args.dir) if args.dir else default_root(os.getcwd())
    found = find_project(root)
    paused = found.dir / PAUSED_FILE if found else None
    if paused is None or not paused.exists():
        print(f"Recording isn't paused for {root}.")
        return 0
    paused.unlink()
    print(f"Recording resumed for {paused.parent.parent}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backtrack", description="Flight recorder for AI coding agents")
    parser.add_argument("--dir", help="project folder (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install", help="enable recording for this project").set_defaults(func=cmd_install)
    imp = sub.add_parser("import", help="import past sessions from Claude Code transcripts")
    imp.add_argument("transcripts", nargs="*",
                     help="transcript .jsonl files (default: all for this project)")
    imp.set_defaults(func=cmd_import)
    sub.add_parser("sessions", help="list recorded sessions").set_defaults(func=cmd_sessions)
    show = sub.add_parser("show", help="show a session's events")
    show.add_argument("session", help="session id or unique prefix")
    show.set_defaults(func=cmd_show)
    search = sub.add_parser("search", help="search prompts, commands, files and output across sessions")
    search.add_argument("query", nargs="*", help="words to find (each matches as a prefix)")
    search.add_argument("--irreversible", action="store_true",
                        help="list all points of no return instead")
    search.add_argument("--limit", type=int, default=50)
    search.set_defaults(func=cmd_search)
    rewind = sub.add_parser("rewind", help="restore a session's files as of turn K into a new folder")
    rewind.add_argument("session", help="session id or unique prefix")
    rewind.add_argument("turn", type=int)
    rewind.add_argument("--in-place", action="store_true",
                        help="restore the session's own folder instead of copying to a new one")
    rewind.set_defaults(func=cmd_rewind)
    undo = sub.add_parser("undo", help="restore the project folder to a snapshot (undo a rewind)")
    undo.add_argument("snapshot", help="snapshot id printed by rewind --in-place")
    undo.set_defaults(func=cmd_undo)
    fork = sub.add_parser("fork", help="redo turn K of a session with a new instruction")
    fork.add_argument("session", help="session id or unique prefix")
    fork.add_argument("turn", type=int, help="the turn to replace")
    fork.add_argument("instruction", help="the new prompt for turn K")
    fork.add_argument("--model", help="model for the forked session (default: Claude Code's)")
    fork.add_argument("--permission-mode", default="acceptEdits",
                      help="Claude Code permission mode (default: acceptEdits)")
    fork.add_argument("--allow", action="append", default=[], metavar="TOOL",
                      help='extra allowed tool, e.g. "Bash(uv run pytest:*)" (repeatable)')
    fork.set_defaults(func=cmd_fork)
    cmp = sub.add_parser("compare", help="compare two sessions' files and test results")
    cmp.add_argument("a", help="session id or unique prefix, e.g. the original")
    cmp.add_argument("b", help="session id or unique prefix, e.g. its fork")
    cmp.add_argument("--test-cmd", help="test command (default: from .backtrack/config.json, "
                     f"else {compare.DEFAULT_TEST_COMMAND!r})")
    cmp.add_argument("--no-tests", action="store_true", help="only compare files")
    cmp.add_argument("--rerun", action="store_true", help="ignore cached test results")
    cmp.set_defaults(func=cmd_compare)
    aud = sub.add_parser("audit", help="find sessions that weakened the tests: deleted, skipped, fewer assertions")
    aud.add_argument("session", nargs="?", help="session id or unique prefix (default: every session)")
    aud.add_argument("--json", action="store_true", help="print the findings as JSON")
    aud.add_argument("--git", metavar="RANGE",
                     help='audit commits instead of sessions, e.g. "main...HEAD" (no recording needed)')
    aud.add_argument("--run-tests", action="store_true",
                     help="run each flagged session's final code with its own tests and with the "
                          "tests it started with (cached)")
    aud.add_argument("--test-cmd", help="test command (default: from .backtrack/config.json)")
    aud.add_argument("--rerun", action="store_true", help="ignore cached test results")
    aud.add_argument("--intended", type=int, nargs="+", default=[], metavar="N",
                     help="mark findings (numbered in the session's list) as intended")
    aud.add_argument("--reopen", type=int, nargs="+", default=[], metavar="N",
                     help="undo --intended for these findings")
    aud.add_argument("--fail-on", choices=audit.SEVERITIES,
                     help="exit with 1 if a finding is this severe or worse")
    aud.set_defaults(func=cmd_audit)
    serve = sub.add_parser("serve", help="open the timeline in a browser")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=cmd_serve)
    opn = sub.add_parser("open", help="open the timeline, starting a server in the background if needed")
    opn.add_argument("--port", type=int, default=8765, help="first port to try (default: 8765)")
    opn.add_argument("--no-browser", action="store_true", help="only print the address")
    opn.set_defaults(func=cmd_open)
    sub.add_parser("doctor", help="check that this project is being recorded").set_defaults(func=cmd_doctor)
    sub.add_parser("pause", help="stop recording this project").set_defaults(func=cmd_pause)
    sub.add_parser("resume", help="record this project again").set_defaults(func=cmd_resume)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
