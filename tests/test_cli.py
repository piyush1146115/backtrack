import json
import subprocess

import pytest

from backtrack.cli import main
from backtrack.install import HOOK_EVENTS, merge_hooks
from backtrack.store.base import Event, Session
from backtrack.store.sqlite import SqliteStore


def test_merge_hooks_keeps_other_hooks_and_is_idempotent():
    other = {"type": "command", "command": "other-tool"}
    settings = {"model": "x", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [other]}]}}
    merge_hooks(settings, "/old/python -m backtrack.hooks")
    merge_hooks(settings, "/new/python -m backtrack.hooks")
    assert settings["model"] == "x"
    assert set(settings["hooks"]) == set(HOOK_EVENTS)
    pre = settings["hooks"]["PreToolUse"]
    assert pre == [
        {"matcher": "Bash", "hooks": [other]},
        {"matcher": "*", "hooks": [{"type": "command", "command": "/new/python -m backtrack.hooks"}]},
    ]
    assert settings["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "/new/python -m backtrack.hooks"}]}]


def test_install_sessions_show(tmp_path, capsys):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    assert main(["--dir", str(root), "install"]) == 0

    settings = json.loads((root / ".claude" / "settings.local.json").read_text())
    assert "backtrack.hooks" in settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "/.backtrack/" in (root / ".git" / "info" / "exclude").read_text()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True).stdout
    assert ".backtrack" not in status

    store = SqliteStore(root / ".backtrack" / "backtrack.db")
    store.ensure_session(Session("abcdef12-3456", "2026-10-28T10:00:00.000+00:00", cwd=str(root)))
    store.append_event(Event("abcdef12-3456", "prompt", input={"prompt": "fix the cache"},
                             snapshot_ref="1234567890"))
    store.append_event(Event("abcdef12-3456", "tool_pre", tool_name="Bash",
                             input={"command": "git push"}, irreversible=True))
    capsys.readouterr()

    assert main(["--dir", str(root), "sessions"]) == 0
    out = capsys.readouterr().out
    assert "abcdef12" in out and "fix the cache" in out

    assert main(["--dir", str(root), "show", "abcd"]) == 0
    out = capsys.readouterr().out
    assert "Turn 1" in out and "[snap 1234567]" in out
    assert "→ Bash: git push" in out and "POINT OF NO RETURN" in out

    assert main(["--dir", str(root), "search", "cache"]) == 0
    out = capsys.readouterr().out
    assert "abcdef12  turn 1" in out and "fix the [cache]" in out and "1 match(es)." in out
    assert main(["--dir", str(root), "search", "--irreversible"]) == 0
    out = capsys.readouterr().out
    assert "⚠ Bash: git push" in out and "1 point(s) of no return." in out


def test_import_command(tmp_path, capsys, monkeypatch):
    from backtrack.importer import transcript_dir
    from tests.test_importer import SID, TRANSCRIPT

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    root.mkdir()
    main(["--dir", str(root), "install"])
    tdir = transcript_dir(root.resolve())
    tdir.mkdir(parents=True)
    (tdir / f"{SID}.jsonl").write_text("\n".join(json.dumps(r) for r in TRANSCRIPT))
    capsys.readouterr()

    assert main(["--dir", str(root), "import"]) == 0
    assert "1 imported, 0 skipped" in capsys.readouterr().out
    assert main(["--dir", str(root), "import"]) == 0
    assert "0 imported, 1 skipped" in capsys.readouterr().out


def test_rewind_command(tmp_path, capsys):
    from backtrack.snapshot.shadow import ShadowRepo

    root = tmp_path / "proj"
    root.mkdir()
    main(["--dir", str(root), "install"])
    home = root.resolve()
    shadow = ShadowRepo(home, home / ".backtrack" / "git")
    store = SqliteStore(home / ".backtrack" / "backtrack.db")
    store.ensure_session(Session("abcdef12-3456", "2026-10-28T10:00:00.000+00:00", cwd=str(home)))
    (home / "app.py").write_text("before\n")
    store.append_event(Event("abcdef12-3456", "prompt", snapshot_ref=shadow.snapshot()))
    store.append_event(Event("abcdef12-3456", "tool_pre", tool_name="Bash",
                             input={"command": "curl -X POST https://x"}, irreversible=True))
    (home / "app.py").write_text("after\n")
    capsys.readouterr()

    assert main(["--dir", str(root), "rewind", "abcd", "1"]) == 0
    out = capsys.readouterr().out
    fork_dir = out.splitlines()[1].strip()
    assert open(f"{fork_dir}/app.py").read() == "before\n"
    assert "Points of no return" in out and "curl -X POST" in out
    assert 'backtrack fork abcdef12 1 "<instruction>"' in out

    # In place, then undo.
    assert main(["--dir", str(root), "rewind", "abcd", "1", "--in-place"]) == 0
    out = capsys.readouterr().out
    assert (home / "app.py").read_text() == "before\n"
    assert "Points of no return" in out
    undo = next(line.split()[-1] for line in out.splitlines() if " undo " in line)
    assert main(["--dir", str(root), "undo", undo]) == 0
    assert (home / "app.py").read_text() == "after\n"
    with pytest.raises(SystemExit, match="No snapshot"):
        main(["--dir", str(root), "undo", "0000000"])


def test_hook_status(tmp_path):
    from backtrack.install import hook_status, write_hook_settings

    assert hook_status(tmp_path) == "missing"
    path = write_hook_settings(tmp_path)
    assert hook_status(tmp_path) == "installed"
    path.write_text(path.read_text().replace("-m backtrack.hooks", "-m backtrack.hooks --old"))
    assert hook_status(tmp_path) == "outdated"
    path.write_text("{not json")
    assert hook_status(tmp_path) == "missing"
