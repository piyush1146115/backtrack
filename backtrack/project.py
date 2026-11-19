"""Locating a Backtrack-enabled project and its .backtrack/ directory."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

BACKTRACK_DIR = ".backtrack"
FORKS_DIR = "forks"


@dataclass(frozen=True)
class Project:
    root: Path  # the work tree that is recorded and snapshotted
    # For a fork, the project whose .backtrack/ holds the shared log and shadow repo.
    home: Path | None = None

    @property
    def is_fork(self) -> bool:
        return self.home is not None

    @property
    def dir(self) -> Path:
        return (self.home or self.root) / BACKTRACK_DIR

    @property
    def db_path(self) -> Path:
        return self.dir / "backtrack.db"

    @property
    def forks_dir(self) -> Path:
        return self.dir / FORKS_DIR

    @property
    def git_dir(self) -> Path:
        if self.is_fork:
            # A fork is a worktree of the shadow repo; its .git file names its git dir.
            return Path((self.root / ".git").read_text().split("gitdir:", 1)[1].strip())
        return self.dir / "git"

    @property
    def error_log(self) -> Path:
        return self.dir / "hook-errors.log"


def find_project(start: str | Path) -> Project | None:
    """Walk up from `start` to the nearest directory containing .backtrack/.

    A folder under <project>/.backtrack/forks/<name> is that fork's own work tree,
    recorded into the parent project's log.
    """
    path = Path(start).resolve()
    for candidate in (path, *path.parents):
        if (candidate / BACKTRACK_DIR).is_dir():
            forks = candidate / BACKTRACK_DIR / FORKS_DIR
            if forks in path.parents:
                fork_root = forks / path.relative_to(forks).parts[0]
                return Project(fork_root, home=candidate)
            return Project(candidate)
    return None


def default_root(cwd: str | Path) -> Path:
    """The git toplevel containing `cwd`, or `cwd` itself outside a git repo."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode == 0:
        return Path(result.stdout.strip())
    return Path(cwd).resolve()
