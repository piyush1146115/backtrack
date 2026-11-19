"""Setting a project up for recording: .backtrack/ and Claude Code hook settings."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from .project import Project
from .snapshot.shadow import ShadowRepo
from .store.sqlite import SqliteStore

HOOK_MODULE = "backtrack.hooks"
# Hook event -> whether it takes a tool matcher.
HOOK_EVENTS = {
    "SessionStart": False,
    "UserPromptSubmit": False,
    "PreToolUse": True,
    "PostToolUse": True,
    "PostToolUseFailure": True,
    "Stop": False,
    "StopFailure": False,
    "SessionEnd": False,
}


# A folder with .backtrack/paused is not recorded.
PAUSED_FILE = "paused"
# The plugin records git repos up to this many tracked files without asking.
MAX_AUTO_FILES = 50_000


def hook_command() -> str:
    return f"{shlex.quote(sys.executable)} -m {HOOK_MODULE}"


def init_project(root: Path) -> Project:
    """Create .backtrack/ with the event store and shadow repo, plus a baseline snapshot."""
    project = Project(root.resolve())
    project.dir.mkdir(exist_ok=True)
    SqliteStore(project.db_path).close()
    shadow = ShadowRepo(project.root, project.git_dir)
    shadow.init()
    shadow.snapshot("baseline")
    exclude_backtrack(project.root)
    return project


def exclude_backtrack(root: Path) -> None:
    """Keep .backtrack/ out of the user's repo without editing tracked files."""
    user_exclude = root / ".git" / "info" / "exclude"
    if user_exclude.parent.is_dir():
        existing = user_exclude.read_text() if user_exclude.exists() else ""
        if "/.backtrack/" not in existing.splitlines():
            with open(user_exclude, "a") as f:
                f.write(("" if existing.endswith("\n") or not existing else "\n") + "/.backtrack/\n")


def merge_hooks(settings: dict, command: str) -> dict:
    """Add Backtrack's hooks to Claude Code settings, replacing any earlier Backtrack entries."""
    hooks = settings.setdefault("hooks", {})
    for event, takes_matcher in HOOK_EVENTS.items():
        groups = []
        for group in hooks.get(event, []):
            kept = [h for h in group.get("hooks", []) if HOOK_MODULE not in h.get("command", "")]
            if kept:
                groups.append({**group, "hooks": kept})
        ours = {"hooks": [{"type": "command", "command": command}]}
        groups.append({"matcher": "*", **ours} if takes_matcher else ours)
        hooks[event] = groups
    return settings


def write_hook_settings(root: Path) -> Path:
    """Merge Backtrack's hooks into <root>/.claude/settings.local.json."""
    settings_path = root / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(exist_ok=True)
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    settings_path.write_text(json.dumps(merge_hooks(settings, hook_command()), indent=2) + "\n")
    return settings_path


def hook_status(root: Path) -> str:
    """'installed', 'outdated' (some events missing, or another Python) or 'missing'."""
    settings_path = root / ".claude" / "settings.local.json"
    try:
        hooks = json.loads(settings_path.read_text()).get("hooks", {})
    except (OSError, ValueError):
        return "missing"
    commands = {event: [h.get("command", "") for group in hooks.get(event, [])
                        for h in group.get("hooks", [])] for event in HOOK_EVENTS}
    ours = [c for cs in commands.values() for c in cs if HOOK_MODULE in c]
    if not ours:
        return "missing"
    current = hook_command()
    if all(current in cs for cs in commands.values()):
        return "installed"
    return "outdated"


def plugin_enabled() -> bool:
    """Whether the Backtrack plugin is enabled in the user's Claude Code settings."""
    path = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"
    try:
        enabled = json.loads(path.read_text()).get("enabledPlugins") or {}
    except (OSError, ValueError, AttributeError):
        return False
    return any(key.split("@")[0] == "backtrack" and on for key, on in enabled.items())


def auto_root(cwd: str | Path) -> tuple[Path | None, str]:
    """The folder the plugin records for `cwd` (its git toplevel), or None and the reason.

    Repos too large to snapshot on every tool call are remembered in
    ~/.backtrack/too-large, so the files aren't counted again on every event.
    """
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None, "not inside a git repository"
    root = Path(out.stdout.strip()).resolve()
    if root == Path.home().resolve() or root.parent == root:
        return None, f"{root} is the home or root folder"
    too_large = Path.home() / ".backtrack" / "too-large"
    hint = "run `backtrack install` to record it anyway"
    if too_large.is_file() and str(root) in too_large.read_text().splitlines():
        return None, f"too many files to record automatically; {hint}"
    files = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True,
                           text=True).stdout.count("\0")
    if files > MAX_AUTO_FILES:
        too_large.parent.mkdir(exist_ok=True)
        with open(too_large, "a") as f:
            f.write(f"{root}\n")
        return None, f"{files} tracked files, more than {MAX_AUTO_FILES}; {hint}"
    return root, ""


def recording_status(root: Path) -> str:
    """How `root` is recorded: 'paused', the project hooks' status ('installed' or
    'outdated'), 'plugin', or 'missing'."""
    if (root / ".backtrack" / PAUSED_FILE).exists():
        return "paused"
    status = hook_status(root)
    if status == "missing" and plugin_enabled() and auto_root(root)[0] is not None:
        return "plugin"
    return status
