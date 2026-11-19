import io
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from backtrack.cli import main as cli
from backtrack.hooks import __main__ as hooks
from backtrack.install import HOOK_EVENTS, init_project, recording_status, write_hook_settings
from backtrack.store.sqlite import SqliteStore

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin"


def test_plugin_hooks_cover_every_recorded_event():
    hooks_json = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    assert set(hooks_json) == set(HOOK_EVENTS)
    for event, takes_matcher in HOOK_EVENTS.items():
        [group] = hooks_json[event]
        assert ("matcher" in group) == takes_matcher
        assert "record.sh" in group["hooks"][0]["command"]


def test_plugin_version_matches_package():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    package = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    assert manifest["version"] == package["version"]
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    assert market["plugins"][0]["source"] == "./plugin"


def test_record_script_without_the_cli(tmp_path):
    run = lambda *args: subprocess.run(["/bin/sh", str(PLUGIN / "hooks" / "record.sh"), *args],
                                       input="{}", capture_output=True, text=True,
                                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    start = run("start")
    assert start.returncode == 0 and "uv tool install" in json.loads(start.stdout)["systemMessage"]
    other = run()
    assert other.returncode == 0 and other.stdout == ""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / "home").mkdir()
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "app.py").write_text("x = 1\n")
    return root


def run_hook(monkeypatch, root, auto=True, sid="s1"):
    payload = {"hook_event_name": "SessionStart", "session_id": sid, "cwd": str(root), "source": "startup"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hooks.main(auto=auto) == 0


def events(root):
    store = SqliteStore(root / ".backtrack" / "backtrack.db")
    try:
        return [e.kind for s in store.list_sessions() for e in store.get_events(s.session_id)]
    finally:
        store.close()


def test_plugin_sets_up_a_git_repo_on_first_use(repo, monkeypatch):
    run_hook(monkeypatch, repo, auto=False)
    assert not (repo / ".backtrack").exists()  # project hooks only record set-up folders
    run_hook(monkeypatch, repo / ".")
    assert events(repo) == ["session_start"]
    assert "/.backtrack/" in (repo / ".git" / "info" / "exclude").read_text()


def test_plugin_leaves_folders_with_their_own_hooks_alone(repo, monkeypatch):
    init_project(repo)
    write_hook_settings(repo)
    run_hook(monkeypatch, repo)
    assert events(repo) == []  # the project's hooks record it, not the plugin too


def test_plugin_skips_non_repos_home_and_paused(repo, tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    run_hook(monkeypatch, plain)
    assert not (plain / ".backtrack").exists()

    monkeypatch.setenv("HOME", str(repo))  # a home folder that is a git repo
    run_hook(monkeypatch, repo)
    assert not (repo / ".backtrack").exists()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert cli(["--dir", str(repo), "pause"]) == 0
    run_hook(monkeypatch, repo)
    assert not (repo / ".backtrack" / "backtrack.db").exists()
    assert recording_status(repo) == "paused"
    assert cli(["--dir", str(repo), "resume"]) == 0
    run_hook(monkeypatch, repo)
    assert events(repo) == ["session_start"]


def test_plugin_skips_large_repos(repo, monkeypatch):
    monkeypatch.setattr("backtrack.install.MAX_AUTO_FILES", 0)
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    run_hook(monkeypatch, repo)
    assert not (repo / ".backtrack").exists()
    assert str(repo.resolve()) in (Path.home() / ".backtrack" / "too-large").read_text()


def test_doctor(repo, monkeypatch, capsys):
    assert cli(["--dir", str(repo), "doctor"]) == 1
    assert "not recording" in capsys.readouterr().out

    settings = Path.home() / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"enabledPlugins": {"backtrack@backtrack": True}}))
    assert recording_status(repo) == "plugin"
    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}" if name != "claude" else None)
    assert cli(["--dir", str(repo), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "recording: the Backtrack plugin" in out and "Claude Code not found" in out

    init_project(repo)
    write_hook_settings(repo)
    assert cli(["--dir", str(repo), "doctor"]) == 0
    assert "recording: this project's hooks" in capsys.readouterr().out


def test_plugin_found_in_claude_config_dir(repo, tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({"enabledPlugins": {"backtrack@backtrack": True}}))
    assert recording_status(repo) == "missing"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    assert recording_status(repo) == "plugin"
