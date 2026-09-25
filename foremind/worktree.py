"""Batch worktrees outside the repo checkouts (DESIGN §1.3, §1.8): `<wt root>/<project slug>/<batch>/<repo-id>`.

wt root: $FOREMIND_WT_ROOT, else ~/.local/share/foremind/wt. A multi-repo batch gets one worktree per repo and its
session starts in the batch directory. A cross-repo upstream gets a detached worktree at
`<batch dir>/_ro/<upstream batch>/<repo-id>` (repo ids cannot start with "_", so no clash); it is read-only by
convention: writes there fall outside owns_paths and are stopped by PreToolUse and the gate, not by file modes.
Only our own registrations are ever touched: no `git worktree prune` of the user's repo (M1-4 r1 N-5).
"""
import os
import shutil
import subprocess
from pathlib import Path

FETCH_TIMEOUT_S = 120


class WorktreeError(Exception):
    pass


def wt_root() -> Path:
    env = os.environ.get("FOREMIND_WT_ROOT")
    return Path(env) if env else Path.home() / ".local" / "share" / "foremind" / "wt"


def batch_dir(project, batch) -> Path:
    return wt_root() / project / batch


def git(path, *args, check=True, timeout=None) -> str:
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})  # never wait on a credential prompt
    except subprocess.TimeoutExpired:
        raise WorktreeError(f"git {' '.join(args)} in {path}: no answer within {timeout} s") from None
    if check and r.returncode:
        raise WorktreeError(f"git {' '.join(args)} in {path}: {r.stderr.strip()}")
    return r.stdout.strip()


def remote_name(repo_path, configured=None) -> str | None:
    """The remote to fetch: the configured one if the repo has it, else origin, else the only one; None if none."""
    names = git(repo_path, "remote").split()
    if configured in names:
        return configured
    if "origin" in names:
        return "origin"
    return names[0] if len(names) == 1 else None


def fetch(repo_path, remote) -> None:
    git(repo_path, "fetch", "--quiet", remote, timeout=FETCH_TIMEOUT_S)


def dirty(path) -> list[str]:
    """Paths (relative to the worktree) with uncommitted changes, untracked files included."""
    r = subprocess.run(["git", "-C", str(path), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                       capture_output=True, text=True)
    if r.returncode:
        raise WorktreeError(f"git status in {path}: {r.stderr.strip()}")
    out, skip = [], False
    for item in r.stdout.split("\0"):
        if skip or not item:
            skip = False
            continue
        out.append(item[3:])
        skip = "R" in item[:2] or "C" in item[:2]  # -z: a rename's original path follows as its own field
    return out


def _registered(repo_path, path) -> bool:
    want = os.path.realpath(path)
    out = git(repo_path, "worktree", "list", "--porcelain")
    return any(os.path.realpath(line[9:]) == want for line in out.splitlines() if line.startswith("worktree "))


def _ensure_registration(repo_path, path) -> bool:
    """Is `path` a live worktree of the repo? A registration whose directory is gone is removed (ours only)."""
    if not _registered(repo_path, path):
        return False
    if Path(path).exists():
        return True
    git(repo_path, "worktree", "remove", "--force", str(path))
    return False


def create(project, batch, repo, *, branch, start_point) -> tuple[Path, str]:
    """Ensure repo's worktree for the batch on `branch`; returns (path, HEAD sha).

    `start_point` (a ref, or a callable returning one) is only used when the branch has to be created: an existing
    worktree (retry, successor) or branch is reused as it is, and the seat's verification compares it to the record.
    """
    path = batch_dir(project, batch) / repo.id
    if not _ensure_registration(repo.path, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if git(repo.path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False):
            git(repo.path, "worktree", "add", str(path), branch)
        else:
            start = start_point() if callable(start_point) else start_point
            git(repo.path, "worktree", "add", "--no-track", "-b", branch, str(path), start)
    return path, git(path, "rev-parse", "HEAD")


def create_readonly(project, batch, upstream, repo, ref) -> tuple[Path, str]:
    """Ensure a detached worktree of `repo` at `ref` for an upstream batch; moves it if the ref moved (§1.8), but
    never over local changes (N-6)."""
    path = batch_dir(project, batch) / "_ro" / upstream / repo.id
    sha = git(repo.path, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if not _ensure_registration(repo.path, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        git(repo.path, "worktree", "add", "--detach", str(path), sha)
    elif git(path, "rev-parse", "HEAD") != sha:
        if changed := dirty(path):
            raise WorktreeError(f"{path}: read-only upstream worktree has local changes {changed[:5]}; not moving it")
        git(path, "checkout", "--detach", sha)
    return path, sha


def remove(project, batch, repos, *, force=False) -> None:
    """Remove the batch's worktrees and directory; branches stay. A dirty worktree stops it unless force=True
    (read-only upstream worktrees are always forced: nothing of the batch's work lives there)."""
    d = batch_dir(project, batch)
    if not d.exists():
        return
    by_id = {r.id: r for r in repos}
    targets = [(p, True) for p in sorted((d / "_ro").glob("*/*"))] + [(d / rid, force) for rid in by_id]
    for p, f in targets:
        if not p.exists():
            continue
        if p.name not in by_id:
            raise WorktreeError(f"{p}: repo {p.name!r} is not in the given repos")
        git(by_id[p.name].path, "worktree", "remove", *(["--force"] if f else []), str(p))
    left = [p for p in d.rglob("*") if not p.is_dir()]
    if left:
        raise WorktreeError(f"{d}: unexpected files left after removing the worktrees: {left[:3]}")
    shutil.rmtree(d)  # only empty directories (_ro/...) remain
