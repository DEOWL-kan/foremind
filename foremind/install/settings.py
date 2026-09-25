"""Our hooks and status line in `<dir>/.claude/settings.local.json` (DESIGN §13.2, §1.1).

Entries come from vendors.claude.settings() and are recognized by their command (`… -m foremind hook <event>` /
`… -m foremind statusline`), so a reinstall with another interpreter replaces them; the user's entries are never
touched. A status line already in effect (local, else project `.claude/settings.json`, else the user's Claude
settings) is wrapped, not replaced: its command goes to the user-level `[statusline] command` (a marked block,
the only layer that key may be set in) and ours takes its place. Only the user's Claude settings are wrapped as is
(§20 I53②): a command from the project's or local settings would then run in every project, so it takes the user's
consent, and without it the status line stays as it is (doctor fails).
The file's bytes before the first install are kept in `.foremind/install/`; uninstall puts them back byte for byte
when nothing but our entries changed since, else it writes the cleaned content. Symlinked files are written through.
"""
import copy
import json
import os
import re
import tomllib
from pathlib import Path

from foremind.fsutil import atomic_write, file_lock, sha256_bytes
from foremind.install import tomlblock
from foremind.paths import state_dir, user_config_dir
from foremind.vendors import claude

OURS = re.compile(r"\s-m foremind (hook \S+|statusline)$")
STATUSLINE_BLOCK = "statusline"
USER_TEMPLATE = "# foremind 用户层配置（DESIGN §1.5）\n"


class SettingsError(Exception):
    pass


def path(d) -> Path:
    return Path(d) / ".claude" / "settings.local.json"


def _manifest(root, d) -> Path:
    return state_dir(root) / "install" / f"settings-{sha256_bytes(str(Path(d).resolve()).encode())[:12]}.json"


def ours(cmd) -> bool:
    return isinstance(cmd, str) and bool(OURS.search(cmd))


def _read(p) -> tuple[bytes | None, dict]:
    raw = p.read_bytes() if p.exists() else None
    try:
        text = (raw or b"").decode("utf-8")
        if text.startswith("\ufeff"):
            raise ValueError("starts with a byte order mark")
        data = json.loads(text) if text.strip() else {}
    except ValueError as e:  # UnicodeDecodeError, JSONDecodeError
        raise SettingsError(f"{p}: not valid UTF-8 JSON ({e}); fix or move it, then rerun") from None
    if not isinstance(data, dict):
        raise SettingsError(f"{p}: not a JSON object")
    return raw, data


def _quiet_read(p) -> dict:
    try:
        return _read(p)[1]
    except (SettingsError, OSError):
        return {}


def _drop_ours(data):
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return
    for ev, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                inner = [h for h in g["hooks"] if not (isinstance(h, dict) and ours(h.get("command")))]
                if len(inner) != len(g["hooks"]):
                    if not inner:
                        continue  # the whole group was ours
                    g = {**g, "hooks": inner}
            kept.append(g)
        hooks[ev] = kept


def installed_commands(d) -> list[str]:
    _, data = _read(path(d))
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    cmds = [h.get("command") for gs in hooks.values() if isinstance(gs, list) for g in gs if isinstance(g, dict)
            for h in (g.get("hooks") if isinstance(g.get("hooks"), list) else []) if isinstance(h, dict)]
    sl = data.get("statusLine")
    cmds.append(sl.get("command") if isinstance(sl, dict) else None)
    return [c for c in cmds if ours(c)]


def _claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _statusline_in_effect(d, local) -> tuple[dict | None, Path | None]:
    """(the status line in effect in `d`, the settings file it comes from)."""
    for src in (path(d), Path(d) / ".claude" / "settings.json", _claude_home() / "settings.json"):
        if isinstance(sl := (local if src == path(d) else _quiet_read(src)).get("statusLine"), dict):
            return sl, src
    return None, None


def _user_level() -> tuple[object, str | None]:
    """(the user's own `statusline` in the user-level config.toml, the command in our block there)."""
    p = user_config_dir() / "config.toml"
    try:
        text = p.read_text(encoding="utf-8")
        mine = tomlblock.get(text, STATUSLINE_BLOCK)
        rest = tomllib.loads(tomlblock.strip(text, STATUSLINE_BLOCK))
        cmd = tomllib.loads(mine)["statusline"]["command"] if mine is not None else None
    except FileNotFoundError:
        return None, None
    except (ValueError, KeyError, TypeError, tomlblock.BlockError) as e:  # ValueError: TOMLDecodeError, UnicodeDecodeError
        raise SettingsError(f"{p}: {e}") from None
    return rest.get("statusline"), cmd


def wrapped() -> tuple[str | None, bool]:
    """(the user-level [statusline] command, whether it sits in our block)."""
    own, mine = _user_level()
    if isinstance(own, dict) and own.get("command") is not None:
        return own["command"], False
    return mine, mine is not None


def _lock():
    return file_lock(user_config_dir() / "config.lock")  # the user-level config.toml's read-modify-write


def _wrap(cmd) -> str | None:
    """Put `cmd` behind our status line; None if done, else why not."""
    with _lock():
        cur, _ = wrapped()
        if cur == cmd:
            return None
        if cur is not None:
            return (f"用户层 [statusline] command 已是 {cur!r}，无法再串联 {cmd!r}：本处保留原状态栏（额度遥测不经这里）；"
                    "需要时把两条合成一条命令写进用户层 [statusline] command 后重跑 init")
        if _user_level()[0] is not None:
            return (f"{user_config_dir() / 'config.toml'} 已有 [statusline] 表但没有 command：本处保留原状态栏；"
                    f"把 command = {tomlblock.value(cmd)} 写进那个表（或删掉它）后重跑 init")
        tomlblock.add(user_config_dir() / "config.toml", STATUSLINE_BLOCK,
                      f"[statusline]\ncommand = {tomlblock.value(cmd)}", template=USER_TEMPLATE)
    return None


def install(root, d, consent=None) -> list[str]:
    """Merge our hooks and status line into `d`'s settings.local.json; returns notes for the user.
    `consent(cmd, source)` -> bool is asked before a status line from the project's or local settings is wrapped
    (None: no one to ask, it is not)."""
    p = path(d)
    raw, data = _read(p)
    m = _manifest(root, d)
    if not m.exists():
        atomic_write(m, json.dumps({"path": str(p), "original": None if raw is None else raw.decode("utf-8")},
                                   ensure_ascii=False))
    _drop_ours(data)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsError(f"{p}: `hooks` is not an object")
    for ev, groups in claude.settings()["hooks"].items():
        if not isinstance(hooks.setdefault(ev, []), list):
            raise SettingsError(f"{p}: hooks.{ev} is not a list")
        hooks[ev].extend(groups)
    notes, mine = [], claude.foremind_command("statusline")
    sl, src = _statusline_in_effect(d, data)
    cmd = sl.get("command") if sl else None
    why = None
    if isinstance(cmd, str) and cmd.strip() and not ours(cmd):
        if src != _claude_home() / "settings.json" and wrapped()[0] != cmd and not (consent and consent(cmd, src)):
            why = (f"状态栏 {cmd!r} 来自 {src}，串联后它会成为所有项目的状态栏，没有同意就不串联：保留原状态栏"
                   "（额度遥测要靠本系统的状态栏）；同意时交互运行 foremind init，或自己把它写进用户层 [statusline] "
                   "command 后重跑 init")
        else:
            why = _wrap(cmd)
    if why:
        notes.append(f"{p}: {why}")
    else:
        data["statusLine"] = {**(sl or {}), "type": "command", "command": mine}
    new = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if raw is None or new.encode("utf-8") != raw:
        atomic_write(p.resolve(), new)
    return notes


def original(root, d) -> tuple[bool, str | None]:
    """(whether the manifest is there, the file's text before the first install; None = there was no file)."""
    m = _manifest(root, d)
    if not m.exists():
        return False, None
    try:
        orig = json.loads(m.read_text(encoding="utf-8"))["original"]
        if orig is not None and not isinstance(orig, str):
            raise TypeError("original is not a string")
    except (ValueError, KeyError, TypeError) as e:  # ValueError: JSONDecodeError, UnicodeDecodeError
        raise SettingsError(f"{m}: broken install manifest ({e}); restore it, or delete it to only drop our entries "
                            f"from {path(d)}") from None
    return True, orig


def uninstall(root, d) -> None:
    """Without a manifest (lost, or never installed here) only our entries go, and an untouched file stays as is."""
    p, m = path(d), _manifest(root, d)
    raw, data = _read(p)
    before = copy.deepcopy(data)
    known, orig_text = original(root, d)
    orig = json.loads(orig_text) if orig_text and orig_text.strip() else {}
    clean = copy.deepcopy(orig)
    _drop_ours(clean)  # a foremind-looking entry the user had went at install; it comes back with the bytes
    orig_hooks = orig.get("hooks") if isinstance(orig.get("hooks"), dict) else {}
    _drop_ours(data)
    if isinstance(hooks := data.get("hooks"), dict):
        for ev in [e for e, gs in hooks.items() if gs == [] and e not in orig_hooks]:
            del hooks[ev]
        if not hooks and "hooks" not in orig:
            del data["hooks"]
    if isinstance(sl := data.get("statusLine"), dict) and ours(sl.get("command")):
        if "statusLine" in orig:
            data["statusLine"] = orig["statusLine"]
        else:
            del data["statusLine"]
    if known and data == clean:
        if orig_text is None and p.is_symlink():
            p.resolve().unlink(missing_ok=True)  # it was a dangling link: the link stays, dangling again
        elif orig_text is None:
            p.unlink(missing_ok=True)
            try:
                p.parent.rmdir()  # ponytail: an empty .claude/ the user made before us goes too
            except OSError:
                pass
        elif raw != orig_text.encode("utf-8"):
            atomic_write(p.resolve(), orig_text)
    elif raw is not None and data != before:
        atomic_write(p.resolve(), json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    m.unlink(missing_ok=True)


def unwrap() -> None:
    """Drop our user-level [statusline] block (call once no enabled project uses it)."""
    with _lock():
        tomlblock.remove(user_config_dir() / "config.toml", STATUSLINE_BLOCK, template=USER_TEMPLATE)


def check(d) -> list[str]:
    """Problems with our entries in `d`'s settings.local.json (doctor)."""
    try:
        _, data = _read(path(d))
    except (SettingsError, OSError) as e:
        return [str(e)]
    probs, hooks = [], data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    for ev, groups in claude.settings()["hooks"].items():
        want = groups[0]["hooks"][0]["command"]
        got = [h.get("command") for g in hooks.get(ev) or [] if isinstance(g, dict)
               for h in g.get("hooks") or [] if isinstance(h, dict) and ours(h.get("command"))]
        if got != [want]:
            probs.append(f"{path(d)}: {ev} 钩子应为一条 {want!r}，实际 {got}")
    sl = data.get("statusLine")
    if not (isinstance(sl, dict) and sl.get("command") == claude.foremind_command("statusline")):
        probs.append(f"{path(d)}: statusLine 不是 foremind statusline（额度遥测靠它）")
    return probs
