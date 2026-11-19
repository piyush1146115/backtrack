"""Workspace snapshots in a shadow git repo.

The shadow repo keeps its git dir in .backtrack/git and uses the project folder as
its work tree, so the user's own .git is never touched. The project's .gitignore
files apply as usual; .backtrack/ itself is excluded.
"""

from __future__ import annotations

import fcntl
import io
import os
import re
import subprocess
import tarfile
from contextlib import contextmanager
from pathlib import Path

REF_RE = re.compile(r"^[0-9a-f]{7,40}$")
MAX_DIFF = 300_000


class ShadowRepo:
    def __init__(self, root: str | Path, git_dir: str | Path):
        self.root = Path(root)
        self.git_dir = Path(git_dir)

    def _git(self, *args: str, check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
        # Drop inherited GIT_* variables (e.g. GIT_DIR, GIT_INDEX_FILE) so they can't
        # redirect commands at the user's repo.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return subprocess.run(
            ["git", f"--git-dir={self.git_dir}", f"--work-tree={self.root}",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
            cwd=self.root, env=env, capture_output=True, text=text, check=check,
        )

    @contextmanager
    def _lock(self):
        # Serialise snapshots from concurrent hooks; git's index.lock would fail them.
        with open(self.git_dir / "backtrack.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def init(self) -> None:
        if (self.git_dir / "HEAD").exists():
            return
        self.git_dir.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q")
        self._git("config", "user.name", "Backtrack")
        self._git("config", "user.email", "backtrack@localhost")
        # Also honour the user's own local excludes, if any.
        user_exclude = self.root / ".git" / "info" / "exclude"
        if user_exclude.is_file():
            self._git("config", "core.excludesFile", str(user_exclude))
        exclude = self.git_dir / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        # Local Claude Code settings hold Backtrack's hooks; they aren't project work.
        exclude.write_text("/.backtrack/\n/.claude/settings.local.json\n")

    def _head(self) -> str | None:
        r = self._git("rev-parse", "--verify", "-q", "HEAD", check=False)
        return r.stdout.strip() or None

    def snapshot(self, message: str = "snapshot") -> str:
        """Commit the current work tree. Returns the commit; reuses HEAD if nothing changed."""
        with self._lock():
            return self._snapshot(message)

    def _snapshot(self, message: str) -> str:
        self._git("add", "-A")
        head = self._head()
        if head and self._git("diff", "--cached", "--quiet", "HEAD", check=False).returncode == 0:
            return head
        self._git("commit", "-q", "--allow-empty", "--no-verify", "-m", message)
        return self._head()

    def restore(self, ref: str) -> str:
        """Make the work tree match `ref`: restore contents, remove files added since.

        Ignored files are left alone. The current state is snapshotted first, so a
        restore can itself be undone: returns that snapshot.
        """
        check_refs(ref)
        with self._lock():
            # Always a new commit, even if nothing changed, so it marks a restore point.
            self._git("add", "-A")
            self._git("commit", "-q", "--allow-empty", "--no-verify", "-m", f"before restore to {ref}")
            before = self._head()
            self._git("read-tree", "-u", "--reset", ref)
            return before

    def restore_points(self, limit: int = 20) -> list[dict]:
        """Snapshots taken just before a restore, newest first: the states an undo goes back to."""
        out = self._git("log", f"-n{limit}", "--grep=^before restore to ", "--format=%H%x09%cI%x09%s",
                        "HEAD", check=False).stdout
        points = []
        for line in out.splitlines():
            ref, ts, subject = line.split("\t", 2)
            points.append({"ref": ref, "ts": ts, "restored_to": subject.rsplit(" ", 1)[-1]})
        return points

    def add_worktree(self, path: str | Path, ref: str, branch: str) -> None:
        """Check out snapshot `ref` into a new folder on a new shadow branch."""
        check_refs(ref)
        with self._lock():
            self._git("worktree", "add", "-q", "-b", branch, str(path), ref)

    def diff(self, from_ref: str, to_ref: str) -> str:
        """Unified diff between two snapshots."""
        check_refs(from_ref, to_ref)
        out = self._git("diff", "--no-color", "--no-ext-diff", "-M", from_ref, to_ref).stdout
        if len(out) > MAX_DIFF:
            out = out[:MAX_DIFF] + f"\n… diff truncated ({len(out) - MAX_DIFF} more chars)\n"
        return out

    def diff_stat(self, from_ref: str, to_ref: str) -> list[dict]:
        """Files changed between two snapshots, with added and deleted line counts."""
        check_refs(from_ref, to_ref)
        out = self._git("diff", "--numstat", "-z", "--no-renames", from_ref, to_ref).stdout
        stats = []
        for entry in out.split("\0"):
            if entry:
                added, deleted, path = entry.split("\t", 2)
                stats.append({"path": path, "added": None if added == "-" else int(added),
                              "deleted": None if deleted == "-" else int(deleted)})
        return stats

    def merge_base(self, a: str, b: str) -> str | None:
        check_refs(a, b)
        return self._git("merge-base", a, b, check=False).stdout.strip() or None

    def files(self, ref: str) -> dict[str, str]:
        """{path: blob id} for every file in a snapshot."""
        check_refs(ref)
        out = self._git("ls-tree", "-r", "-z", ref).stdout
        files = {}
        for entry in out.split("\0"):
            if entry:
                meta, path = entry.split("\t", 1)
                if meta.split()[1] == "blob":
                    files[path] = meta.split()[2]
        return files

    def blob(self, blob_id: str) -> bytes:
        check_refs(blob_id)
        return self._git("cat-file", "blob", blob_id, text=False).stdout

    def export(self, ref: str, dest: str | Path) -> None:
        """Write a snapshot's files into `dest`, without touching the work tree or index."""
        check_refs(ref)
        archive = self._git("archive", "--format=tar", ref, text=False).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(dest, filter="data")
            else:
                tar.extractall(dest)


def check_refs(*refs: str) -> None:
    for ref in refs:
        if not REF_RE.match(ref or ""):  # also keeps refs from being read as options
            raise ValueError(f"not a snapshot ref: {ref!r}")
