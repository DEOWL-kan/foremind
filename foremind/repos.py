"""Repositories of a project (DESIGN §1.8)."""
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from foremind.config import ConfigError

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")  # DESIGN §20 I28


@dataclass
class Repo:
    id: str
    path: Path
    remote: str | None = None
    default_branch: str | None = None


def load_repos(root, config: dict) -> list[Repo]:
    """Repos from `[[repos]]`. The root is resolved (absolute, symlinks followed) before the escape check;
    entry paths are only normalized literally (`..` collapsed, symlinks not followed)."""
    root = Path(root).resolve()
    entries = config.get("repos", [])
    if not isinstance(entries, list) or not all(isinstance(r, dict) for r in entries):
        raise ConfigError("repos: write an array of tables ([[repos]]), not a single table")
    if not entries:
        return [Repo(id="main", path=root)] if (root / ".git").exists() else []
    repos, seen = [], set()
    for r in entries:
        rid, path = r.get("id"), r.get("path")
        if not isinstance(rid, str) or not _ID.fullmatch(rid) or not isinstance(path, str) or not path:
            raise ConfigError(f"repos: each entry needs an id matching [A-Za-z0-9][A-Za-z0-9_-]* and a path, got {r!r}")
        if rid in seen:
            raise ConfigError(f"repos: duplicate id {rid!r}")
        seen.add(rid)
        full = Path(os.path.normpath(root / path))  # absolute paths win over the root in `/`
        if not Path(path).is_absolute() and not full.is_relative_to(os.path.normpath(root)):
            raise ConfigError(f"repos: path {path!r} of {rid!r} escapes the project root")
        repos.append(Repo(rid, full, r.get("remote"), r.get("default_branch")))
    return repos


def split_qualified(p: str, repos) -> tuple[str, str]:
    """`<repo-id>:<path>` -> (id, normalized path). The prefix is required even with a single repo."""
    ids = [r.id for r in repos]
    rid, sep, path = p.partition(":")  # first colon only: the path may contain more
    if not sep:
        raise ValueError(f"{p!r}: write <repo-id>:<path> (known repos: {ids})")
    if rid not in ids:
        raise ValueError(f"{p!r}: unknown repo {rid!r} (known: {ids})")
    pp = PurePosixPath(path)
    if not path or pp.is_absolute() or ".." in pp.parts:
        raise ValueError(f"{p!r}: path must be relative and stay inside the repo")
    return rid, pp.as_posix()
