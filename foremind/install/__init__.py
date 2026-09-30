"""Install steps shared by `foremind init`, `doctor` and `uninstall` (DESIGN §13, §1.3, §1.8).

What init writes, all removable by uninstall:
- `.foremind/config.toml`: a marked `project` block with what the user's own project config (foremind.toml, or that
  file outside the block) does not set yet: `[project] name`, `[carrier] kind`, `[notify] channel`, `[[repos]]`.
  foremind.toml is never written: it may be committed, and repo paths are this machine's.
- `.git/info/exclude` of each repo holding the project root or a settings file: a marked block with `.foremind/`
  and `.claude/settings.local.json` (anchored to where they are).
- `.foremind/.gitignore` (`*`), which uninstall leaves: the data it keeps stays ignored (§20 I53④).
- hooks and status line in `.claude/settings.local.json` of the project root and every repo (install.settings).
- the project's line in the user-level `projects` registry (one absolute path per line).
"""
import json
import subprocess
import tomllib
from pathlib import Path

from foremind.config import ConfigError
from foremind.fsutil import atomic_write, file_lock
from foremind.install import settings, tomlblock
from foremind.paths import state_dir, user_config_dir
from foremind.repos import load_repos

PROJECT_BLOCK, EXCLUDE_BLOCK = "project", "exclude"
PROJECT_TEMPLATE = "# foremind 项目配置（本机，在 .foremind/ 里不提交；要与他人共享的写仓库根 foremind.toml）\n"


class InstallError(Exception):
    pass


# --- projects registry --------------------------------------------------------------

def registry_path() -> Path:
    return user_config_dir() / "projects"


def registered() -> list[str]:
    try:
        lines = registry_path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except UnicodeDecodeError as e:
        raise InstallError(f"{registry_path()}: not UTF-8 ({e}); fix or move it, then rerun") from None
    return [x.strip() for x in lines if x.strip() and not x.lstrip().startswith("#")]


def set_registered(root, on: bool) -> None:
    root = str(Path(root).resolve())
    with file_lock(user_config_dir() / "projects.lock"):
        cur = registered()
        new = [p for p in cur if p != root] + ([root] if on else [])
        if new == cur:
            return
        if new:
            atomic_write(registry_path(), "\n".join(new) + "\n")
        else:
            registry_path().unlink(missing_ok=True)


# --- git --------------------------------------------------------------------------------

def git(cwd, *args) -> str | None:
    try:
        p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def toplevel(d) -> Path | None:
    out = git(d, "rev-parse", "--show-toplevel")
    return Path(out).resolve() if out else None


def exclude_file(top) -> Path:
    out = git(top, "rev-parse", "--git-path", "info/exclude")
    if out is None:
        raise InstallError(f"{top}: cannot find .git/info/exclude")
    return Path(out) if Path(out).is_absolute() else Path(top) / out


# --- project config -----------------------------------------------------------------------

def project_config(root) -> Path:
    return state_dir(root) / "config.toml"


def user_part(root) -> dict:
    """The user's own project config: foremind.toml plus .foremind/config.toml outside our block."""
    out = {}
    for p, strip in ((Path(root) / "foremind.toml", False), (project_config(root), True)):
        try:
            text = p.read_text(encoding="utf-8")
            out.update(tomllib.loads(tomlblock.strip(text, PROJECT_BLOCK) if strip else text))
        except FileNotFoundError:
            pass
        except (ValueError, tomlblock.BlockError) as e:
            raise InstallError(f"{p}: {e}") from None
    return out


def our_block(root) -> dict:
    """What an earlier init registered: our block in .foremind/config.toml."""
    p = project_config(root)
    try:
        body = tomlblock.get(p.read_text(encoding="utf-8"), PROJECT_BLOCK)
        return tomllib.loads(body) if body else {}
    except FileNotFoundError:
        return {}
    except (ValueError, tomlblock.BlockError) as e:
        raise InstallError(f"{p}: {e}") from None


def project_repos(root) -> list:
    """Registered repos without going through delivery.toml (which may be the broken part)."""
    data = {}
    for p in (Path(root) / "foremind.toml", project_config(root)):
        try:
            data.update(tomllib.loads(p.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except ValueError as e:
            raise InstallError(f"{p}: {e}") from None
    return load_repos(root, data)


def repo_entry(root, rid, path, remote=None, default_branch=None) -> dict:
    path, root = Path(path).resolve(), Path(root).resolve()
    e = {"id": rid, "path": path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)}
    if remote:
        e["remote"] = remote
    if default_branch:
        e["default_branch"] = default_branch
    return e


def write_project(root, *, name, carrier, notify, repos: list[dict] | None) -> list[str]:
    """Our block with what the user's config lacks; `repos` None = keep the user's [[repos]]."""
    have, lines, notes = user_part(root), [], []
    for table, key, v in (("project", "name", name), ("carrier", "kind", carrier), ("notify", "channel", notify)):
        cur = have.get(table)
        if cur is None:
            lines += [f"[{table}]", f"{key} = {tomlblock.value(v)}", ""]
        elif not isinstance(cur, dict) or cur.get(key) != v:
            notes.append(f"你的项目配置已有 [{table}]，没有写入 {key} = {v!r}（要改请自己改那里）")
    for r in repos or []:
        lines += ["[[repos]]", *(f"{k} = {tomlblock.value(x)}" for k, x in r.items()), ""]
    if lines:
        tomlblock.add(project_config(root), PROJECT_BLOCK, "\n".join(lines), template=PROJECT_TEMPLATE,
                      before=lambda p, t: settings.record(root, p, t))
    else:
        tomlblock.remove(project_config(root), PROJECT_BLOCK, template=PROJECT_TEMPLATE,
                         before=lambda p, t: settings.record(root, p, t))
    return notes


# --- excludes and settings --------------------------------------------------------------------

def settings_dirs(root, repos) -> list[Path]:
    """Where hooks go: the project root and every repo."""
    out = []
    for d in (Path(root), *(r.path for r in repos)):
        if (d := d.resolve()) not in out:
            out.append(d)
    return out


def _anchored(top, d, rel) -> str:
    sub = d.relative_to(top).as_posix()
    return "/" + (rel if sub == "." else f"{sub}/{rel}")


def tracked_settings(dirs) -> list[Path]:
    """settings.local.json files git already tracks: an exclude does not keep them out of commits."""
    return [settings.path(d) for d in dirs
            if git(d, "ls-files", "--error-unmatch", "--", ".claude/settings.local.json") is not None]


def write_gitignore(root) -> None:
    if not (gi := state_dir(root) / ".gitignore").exists():
        atomic_write(gi, "*\n")


def write_excludes(root, dirs) -> None:
    root, groups = Path(root).resolve(), {}
    write_gitignore(root)
    for d in dirs:
        if (top := toplevel(d)) is None:
            continue
        pats = groups.setdefault(top, [])
        pats.append(_anchored(top, d, ".claude/settings.local.json"))
        if d == root:
            pats.append(_anchored(top, d, ".foremind/"))
    for top, pats in groups.items():
        tomlblock.add(exclude_file(top), EXCLUDE_BLOCK, "\n".join(dict.fromkeys(pats)), check=False)


def installed_dirs(root) -> list[Path]:
    """Directories whose settings.local.json we changed (the manifests in .foremind/install/)."""
    out = []
    for m in sorted((state_dir(root) / "install").glob("settings-*.json")):
        try:
            out.append(Path(json.loads(m.read_text(encoding="utf-8"))["path"]).parent.parent)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def uninstall(root) -> list[str]:
    """Also the registered repos without a manifest (lost): our hook entries there would keep the project on."""
    root = Path(root).resolve()
    try:
        repos = project_repos(root)
    except (InstallError, ConfigError):
        repos = []
    dirs = list(dict.fromkeys([*settings_dirs(root, repos), *installed_dirs(root)]))
    tops = dict.fromkeys(t for d in dirs if (t := toplevel(d)) is not None)
    excludes = [exclude_file(t) for t in tops]
    for d in dirs:  # an unreadable file stops us before anything changed
        settings.installed_commands(d)
        settings.original(root, d)
    for p in excludes:
        tomlblock.remove(p, EXCLUDE_BLOCK, check=False, dry_run=True)
    tomlblock.remove(project_config(root), PROJECT_BLOCK, template=PROJECT_TEMPLATE, dry_run=True)
    settings.wrapped()
    registered()
    for d in dirs:
        settings.uninstall(root, d)
    for p in excludes:
        if tomlblock.remove(p, EXCLUDE_BLOCK, check=False) and not p.read_text(encoding="utf-8").strip():
            p.unlink()  # the repo had no exclude file (or an empty one) before init
    tomlblock.remove(project_config(root), PROJECT_BLOCK, template=PROJECT_TEMPLATE,
                     before=lambda p, t: settings.record(root, p, t))
    set_registered(root, False)
    if not registered():
        settings.unwrap()
    return [f"保留了 {state_dir(root)}（计划、批次、事件等数据，其中的 .gitignore 让 git 继续忽略它）；"
            f"不再需要时自己删除：rm -rf {state_dir(root)}"]
