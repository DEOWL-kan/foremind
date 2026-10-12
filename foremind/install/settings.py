"""Our hooks and status line in `<dir>/.claude/settings.local.json` (DESIGN §13.2, §1.1).

Entries come from vendors.claude.settings(); what install adds is recorded in `.foremind/install/` (manifest()):
whether the file, the `hooks` key and each event key were new, each hook group as its JSON value, and the status
line it wrote — never the file's text (it may hold env secrets). A reinstall takes the recorded entries out first;
uninstall takes out only those equal to the record, value for value, and the keys and file recorded as new once
empty. Entries that look like ours (by command, `… -m foremind hook <event>` / `… -m foremind statusline`) but are
not recorded are reported, never touched; without a readable manifest uninstall leaves the file as it is.
A local status line is left as it is (not wrapped). Without one, the status line in effect (project
`.claude/settings.json`, else the user's Claude settings) is wrapped: its command goes to the user-level
`[statusline] command` (a marked block, the only layer that key may be set in) and ours takes its place. Only the
user's Claude settings are wrapped as is (§20 I53②): the project's would then run in every project, so it takes the
user's consent, and without it the status line stays as it is (doctor fails). Symlinked files are written through.
"""
import copy
import json
import os
import re
import tomllib
from pathlib import Path

from foremind.events import EventLog
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


def record(root, p, text) -> None:
    """Before we write a #22 file (§9.2 L0): the hash of what it will hold (None: deleted), so L0 takes the change
    as the program's, not as one without approval."""
    EventLog(state_dir(root) / "events.jsonl").append(
        "program_config_write", path=os.path.realpath(p), by="install",
        sha256=None if text is None else sha256_bytes(text.encode("utf-8")))


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


def _commands(data) -> list[str]:
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    cmds = [h.get("command") for gs in hooks.values() if isinstance(gs, list) for g in gs if isinstance(g, dict)
            for h in (g.get("hooks") if isinstance(g.get("hooks"), list) else []) if isinstance(h, dict)]
    sl = data.get("statusLine")
    cmds.append(sl.get("command") if isinstance(sl, dict) else None)
    return [c for c in cmds if ours(c)]


def installed_commands(d) -> list[str]:
    return _commands(_read(path(d))[1])


def _claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _statusline_in_effect(d) -> tuple[dict | None, Path | None]:
    """(the status line in effect in `d` when its settings.local.json has none, the settings file it comes from)."""
    for src in (Path(d) / ".claude" / "settings.json", _claude_home() / "settings.json"):
        if isinstance(sl := _quiet_read(src).get("statusLine"), dict):
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


def _same(a, b) -> bool:
    """Value for value, as JSON has them (Python's == takes true for 1)."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _ours_group(g) -> bool:
    return (isinstance(g, dict) and isinstance(g.get("hooks"), list) and bool(g["hooks"])
            and all(isinstance(h, dict) and ours(h.get("command")) for h in g["hooks"]))


def _valid(man) -> bool:
    return (isinstance(man, dict) and isinstance(man.get("path"), str) and isinstance(man.get("created"), bool)
            and isinstance(man.get("hooks_created"), bool) and isinstance(man.get("hooks"), dict) and "statusLine" in man
            and all(isinstance(r, dict) and isinstance(r.get("created"), bool) and isinstance(r.get("groups"), list)
                    for r in man["hooks"].values()))


def _convert(p, old) -> tuple[dict, list[str]]:
    """An older manifest (the file's text before the first install, `original`) as the new one: what the file holds
    of ours now and is not in that text, and which keys that text did not have."""
    text = old.get("original")
    try:
        orig = json.loads(text) if isinstance(text, str) and text.strip() else {}
    except ValueError:
        orig = {}
    orig = orig if isinstance(orig, dict) else {}
    oh = orig.get("hooks") if isinstance(orig.get("hooks"), dict) else {}
    _, data = _read(p)
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    man = {"path": str(p), "created": text is None, "hooks_created": "hooks" not in orig, "hooks": {},
           "statusLine": None}
    for ev, groups in hooks.items():
        before = oh.get(ev) if isinstance(oh.get(ev), list) else []
        mine = [g for g in groups if _ours_group(g) and not any(_same(g, o) for o in before)] \
            if isinstance(groups, list) else []
        if mine or (ev not in oh and ev in claude.settings()["hooks"] and isinstance(groups, list)):
            man["hooks"][ev] = {"created": ev not in oh, "groups": mine}  # an older install made each of our keys
    sl, notes = data.get("statusLine"), []
    if isinstance(sl, dict) and ours(sl.get("command")):
        man["statusLine"] = sl
    if "statusLine" in orig and not _same(orig["statusLine"], sl):
        o = orig["statusLine"]
        notes.append(f"{p}: 安装前这里的本地状态栏是 {o.get('command') if isinstance(o, dict) else o!r}；"
                     "新的安装清单不再保存它，卸载后不会放回，需要时自己写回去")
    return man, notes


def manifest(root, d) -> dict | None:
    """`d`'s install manifest, None when there is none; a broken one raises. An older one is rewritten first, its notes
    printed then: the next run has nothing left to say them from."""
    m = _manifest(root, d)
    if not m.exists():
        return None
    try:
        man = json.loads(m.read_text(encoding="utf-8"))
        if isinstance(man, dict) and "original" in man:
            man, notes = _convert(path(d), man)
            atomic_write(m, json.dumps(man, ensure_ascii=False))
            for n in notes:
                print(f"注意：{n}", flush=True)
            return man
        if not _valid(man):
            raise ValueError("unknown shape")
    except ValueError as e:  # JSONDecodeError, UnicodeDecodeError
        raise SettingsError(f"{m}: broken install manifest ({e})") from None
    return man


def _remove(p, data, man) -> list[str]:
    """Take what `man` recorded out of `data`, only entries equal to the record; notes on the ones that are not."""
    notes, hooks = [], data.get("hooks")
    for ev, r in man["hooks"].items():
        groups = hooks.get(ev) if isinstance(hooks, dict) else None
        for g in r["groups"]:
            i = next((i for i in reversed(range(len(groups))) if _same(groups[i], g)), None) \
                if isinstance(groups, list) else None
            if i is None:
                notes.append(f"{p}: hooks.{ev} 里没有与安装记录相同的钩子组 {json.dumps(g, ensure_ascii=False)}"
                             "（改过或已删）：没有删")
            else:
                del groups[i]
        if r["created"] and groups == []:
            del hooks[ev]
    if man["hooks_created"] and hooks == {}:
        del data["hooks"]
    if man["statusLine"] is not None and "statusLine" in data:
        if _same(data["statusLine"], man["statusLine"]):
            del data["statusLine"]
        else:
            notes.append(f"{p}: statusLine 与安装记录不同（改过）：没有删")
    return notes


def _strays(p, data) -> list[str]:
    """Notes on what looks like ours in `data` but no manifest recorded: left as it is."""
    return [f"{p}: {c!r} 看起来属于本程序，但不是安装记录的条目：没有删也没有改，不需要时自己删除"
            for c in _commands(data)]


def install(root, d, consent=None) -> list[str]:
    """Merge our hooks and status line into `d`'s settings.local.json; returns notes for the user.
    `consent(cmd, source)` -> bool is asked before a status line from the project's settings is wrapped
    (None: no one to ask, it is not)."""
    p = path(d)
    raw, data = _read(p)
    try:
        man, notes = manifest(root, d), []
    except SettingsError as e:
        raise SettingsError(f"{e}：修好它后重跑 init；或先自己删掉 {p} 里本程序的钩子与状态栏，再删掉这个清单重跑"
                            "（只删清单会被当成丢失，钩子装成两条）") from None
    if man is None:
        man = {"path": str(p), "created": raw is None, "hooks_created": "hooks" not in data, "hooks": {},
               "statusLine": None}
    else:
        notes += _remove(p, data, man)  # a reinstall: what the last one recorded first
    notes += _strays(p, data)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsError(f"{p}: `hooks` is not an object")
    new = {"path": str(p), "created": man["created"], "hooks_created": man["hooks_created"], "hooks": {},
           "statusLine": None}
    for ev, groups in claude.settings()["hooks"].items():
        created = man["hooks"][ev]["created"] if ev in man["hooks"] else ev not in hooks
        if not isinstance(hooks.setdefault(ev, []), list):
            raise SettingsError(f"{p}: hooks.{ev} is not a list")
        hooks[ev].extend(groups)
        new["hooks"][ev] = {"created": created, "groups": groups}
    mine = claude.foremind_command("statusline")
    if "statusLine" in data:
        notes.append(f"{p}: 这个目录已有本地状态栏，没有串联；这个目录的会话不经本系统状态栏，读不到额度遥测")
    else:
        sl, src = _statusline_in_effect(d)
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
            data["statusLine"] = new["statusLine"] = {**(sl or {}), "type": "command", "command": mine}
    atomic_write(_manifest(root, d), json.dumps(new, ensure_ascii=False))  # before the file: never an unrecorded entry
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if raw is None or text.encode("utf-8") != raw:
        record(root, p, text)
        atomic_write(p.resolve(), text)
    return notes


def uninstall(root, d) -> list[str]:
    """Only what the manifest recorded goes, entry by entry; without a readable one the file is left as it is.
    Returns notes for the user."""
    p, m = path(d), _manifest(root, d)
    raw, data = _read(p)
    broken = None
    try:
        man, notes = manifest(root, d), []
    except SettingsError as e:
        man, notes, broken = None, [], str(e)
    if man is None:
        if (cmds := _commands(data)) or broken:
            notes.append(f"{broken or f'{m}: 没有安装清单'}：没有改 {p}"
                         + ("；看起来属于本程序的条目请自己删除：" + "、".join(map(repr, cmds)) if cmds else ""))
        return notes
    before = copy.deepcopy(data)
    notes += _remove(p, data, man)
    if data == {} and man["created"]:
        record(root, p, None)
        if p.is_symlink():
            p.resolve().unlink(missing_ok=True)  # it was a dangling link: the link stays, dangling again
        else:
            p.unlink(missing_ok=True)
            try:
                p.parent.rmdir()  # ponytail: an empty .claude/ the user made before us goes too
            except OSError:
                pass
    elif raw is not None and data != before:
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        record(root, p, text)
        atomic_write(p.resolve(), text)
    m.unlink(missing_ok=True)
    return notes


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
