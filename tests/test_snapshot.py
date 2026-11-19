import subprocess
import threading

import pytest

from backtrack.snapshot.shadow import ShadowRepo


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def project(tmp_path):
    """A user project with its own git repo, one commit and a .gitignore."""
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text("*.log\n")
    (root / "app.py").write_text("v1\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "user commit")
    return root


@pytest.fixture
def shadow(project):
    repo = ShadowRepo(project, project / ".backtrack" / "git")
    repo.init()
    return repo


def tree(shadow, ref):
    return set(shadow._git("ls-tree", "-r", "--name-only", ref).stdout.split())


def test_snapshot_respects_gitignore_and_excludes_backtrack(project, shadow):
    (project / "debug.log").write_text("noise")
    ref = shadow.snapshot()
    files = tree(shadow, ref)
    assert files == {".gitignore", "app.py"}


def test_user_repo_untouched(project, shadow):
    head_before = git(project, "rev-parse", "HEAD")
    log_before = git(project, "log", "--oneline")
    (project / "app.py").write_text("v2\n")
    shadow.snapshot()
    assert git(project, "rev-parse", "HEAD") == head_before
    assert git(project, "log", "--oneline") == log_before
    # The user's change is still just an unstaged modification in their repo.
    # (.backtrack/ shows as untracked here; `backtrack install` excludes it.)
    status = git(project, "status", "--porcelain", "--", "app.py")
    assert status == "M app.py"


def test_unchanged_snapshot_reuses_ref(shadow):
    assert shadow.snapshot() == shadow.snapshot()


def test_changed_snapshot_gets_new_ref(project, shadow):
    first = shadow.snapshot()
    (project / "app.py").write_text("v2\n")
    assert shadow.snapshot() != first


def test_restore_contents_and_removes_new_files(project, shadow):
    ref = shadow.snapshot()
    (project / "app.py").write_text("broken\n")
    (project / "new.py").write_text("added later\n")
    (project / "keep.log").write_text("ignored file")
    shadow.restore(ref)
    assert (project / "app.py").read_text() == "v1\n"
    assert not (project / "new.py").exists()
    assert (project / "keep.log").read_text() == "ignored file"


def test_restore_is_undoable(project, shadow):
    ref = shadow.snapshot()
    (project / "app.py").write_text("v2\n")
    before_restore = shadow.restore(ref)
    assert before_restore == shadow._git("rev-parse", "HEAD").stdout.strip()
    # The pre-restore state was captured, so it can be restored in turn.
    assert shadow._git("show", f"{before_restore}:app.py").stdout == "v2\n"


def test_concurrent_snapshots_do_not_collide(project, shadow):
    errors = []

    def snap(i):
        try:
            (project / f"f{i}.txt").write_text(str(i))
            shadow.snapshot(f"s{i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=snap, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert {f"f{i}.txt" for i in range(8)} <= tree(shadow, shadow.snapshot())


def test_diff(project, shadow):
    first = shadow.snapshot()
    (project / "app.py").write_text("v2\n")
    second = shadow.snapshot()
    diff = shadow.diff(first, second)
    assert "--- a/app.py" in diff and "-v1" in diff and "+v2" in diff
    assert shadow.diff(second, second) == ""


@pytest.mark.parametrize("bad", ["--output=/tmp/x", "HEAD", "abc", "zzzzzzz"])
def test_diff_rejects_non_refs(shadow, bad):
    with pytest.raises(ValueError):
        shadow.diff(bad, bad)


def test_worktree_fork_snapshots_independently(project, shadow):
    from backtrack.project import find_project

    (project / ".backtrack" / "forks").mkdir()
    base = shadow.snapshot()
    main_head = shadow._git("rev-parse", "HEAD").stdout.strip()
    fork_dir = project / ".backtrack" / "forks" / "f1"
    shadow.add_worktree(fork_dir, base, "fork/f1")
    assert (fork_dir / "app.py").read_text() == "v1\n"

    fork = find_project(fork_dir)
    assert fork.is_fork and fork.root == fork_dir.resolve() and fork.home == project.resolve()
    assert fork.db_path == (project / ".backtrack" / "backtrack.db").resolve()
    fork_shadow = ShadowRepo(fork.root, fork.git_dir)
    (fork_dir / "app.py").write_text("fork\n")
    fork_ref = fork_shadow.snapshot("in fork")
    assert fork_ref != base and "+fork" in shadow.diff(base, fork_ref)

    # The main work tree and its branch are unaffected, and forks/ is never captured.
    assert (project / "app.py").read_text() == "v1\n"
    assert shadow.snapshot() == main_head
    assert not any(f.startswith(".backtrack") for f in tree(shadow, main_head))


def test_find_project_inside_fork_subfolder(tmp_path):
    from backtrack.project import find_project

    sub = tmp_path / ".backtrack" / "forks" / "f2" / "pkg"
    sub.mkdir(parents=True)
    p = find_project(sub)
    assert p.root == (tmp_path / ".backtrack" / "forks" / "f2").resolve()
    assert p.home == tmp_path.resolve()


def test_files_export_and_stats(project, shadow, tmp_path):
    base = shadow.snapshot()
    (project / "app.py").write_text("v2\nmore\n")
    (project / "pkg").mkdir()
    (project / "pkg" / "new.py").write_text("x\n")
    ref = shadow.snapshot()
    files = shadow.files(ref)
    assert set(files) == {".gitignore", "app.py", "pkg/new.py"}
    assert shadow.blob(files["app.py"]) == b"v2\nmore\n"
    assert shadow.merge_base(base, ref) == base
    assert sorted((s["path"], s["added"], s["deleted"]) for s in shadow.diff_stat(base, ref)) == [
        ("app.py", 2, 1), ("pkg/new.py", 1, 0)]
    out = tmp_path / "out"
    shadow.export(base, out)
    assert (out / "app.py").read_text() == "v1\n" and not (out / "pkg").exists()
    assert (project / "app.py").read_text() == "v2\nmore\n"  # work tree untouched


def test_restore_points(project, shadow):
    assert shadow.restore_points() == []
    first = shadow.snapshot()
    (project / "app.py").write_text("v2\n")
    before = shadow.restore(first)
    [point] = shadow.restore_points()
    assert (point["ref"], point["restored_to"]) == (before, first)
    assert shadow._git("show", f"{point['ref']}:app.py").stdout == "v2\n"
