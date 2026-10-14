"""Our hooks and status line in `<dir>/.claude/settings.local.json` (DESIGN §13.2, §1.1).

Entries come from vendors.claude.settings(); what install adds is recorded in `.foremind/install/` (manifest_path()):
`format_version` (1; none = v0), whether the file, the `hooks` key and each event key were new, each hook group as its
JSON value, and the status line it wrote — never the file's text (it may hold env secrets). An older manifest (v0:
the same without format_version, or the file's text before the first install as `original`) is read as the current
one in memory only (load()); a newer or illegal format_version is never read as an older one. A reinstall puts this
CLI's entries in place of the recorded ones and adds what is not recorded yet; a recorded entry no longer there as
recorded (changed or removed) stops it before anything is written: neither taken over nor doubled (REQ-6). Uninstall
takes out only the entries equal to the record, value for value, and the keys and file recorded as new once empty.
Entries that look like ours (by command, `… -m foremind hook <event>` / `… -m foremind statusline`) but are not
recorded are reported, never touched; without a readable manifest uninstall leaves the file as it is.
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
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import foremind
from foremind.events import EventLog
from foremind.fsutil import atomic_write, file_lock, sha256_bytes
from foremind.install import tomlblock
from foremind.paths import state_dir, user_config_dir
from foremind.vendors import claude

OURS = re.compile(r"\s-m foremind (hook \S+|statusline)$")
STATUSLINE_BLOCK = "statusline"
USER_TEMPLATE = "# foremind 用户层配置（DESIGN §1.5）\n"
FORMAT_VERSION = 1  # the install manifest's; nothing else carries one (REQ-6)


class SettingsError(Exception):
    pass


class ManifestError(SettingsError):
    """An install manifest that cannot be read as one: kind "broken" (not JSON, unknown shape), "illegal" (a
    format_version that is not a whole number from 1) or "future" (newer than FORMAT_VERSION)."""

    def __init__(self, msg, kind):
        super().__init__(msg)
        self.kind = kind


def path(d) -> Path:
    return Path(d) / ".claude" / "settings.local.json"


def manifest_path(root, d) -> Path:
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


def _dump(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


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


def parse(m) -> tuple[int, dict]:
    """(format version, the manifest as stored) of the manifest file `m`, read only; ManifestError when it is not one
    this program can read."""
    try:
        man = json.loads(Path(m).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:  # JSONDecodeError, UnicodeDecodeError
        raise ManifestError(f"{m}: broken install manifest ({e})", "broken") from None
    if not isinstance(man, dict):
        raise ManifestError(f"{m}: broken install manifest (not a JSON object)", "broken")
    if "format_version" not in man:  # v0: per value as now, or an older one with the file's text in `original`
        if not (isinstance(man.get("path"), str) if "original" in man else _valid(man)):
            raise ManifestError(f"{m}: broken install manifest (unknown shape)", "broken")
        return 0, man
    v = man["format_version"]
    if type(v) is not int or v < 1:  # bool is an int to isinstance
        raise ManifestError(f"{m}: format_version {json.dumps(v)} is not a format version (a whole number from 1)",
                            "illegal")
    if v > FORMAT_VERSION:
        raise ManifestError(f"{m}: install manifest format_version {v} is newer than this Foremind reads "
                            f"({FORMAT_VERSION})", "future")
    if not _valid(man):
        raise ManifestError(f"{m}: broken install manifest (unknown shape)", "broken")
    return v, man


def scan(root) -> list[tuple[Path, tuple[int, dict] | ManifestError]]:
    """Every manifest in .foremind/install/, also those of directories no longer registered, with parse()'s result
    or its error. Reads only."""
    out = []
    for m in sorted((state_dir(root) / "install").glob("settings-*.json")):
        try:
            out.append((m, parse(m)))
        except ManifestError as e:
            out.append((m, e))
    return out


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


def load(root, d) -> tuple[dict | None, list[str]]:
    """`d`'s install manifest in the current shape, (None, []) when there is none; ManifestError as parse(). Reads
    only: an older one is converted in memory, with the notes the conversion has for the user (the caller that
    rewrites or removes the manifest says them: after that nothing is left to say them from)."""
    m = manifest_path(root, d)
    if not m.exists():
        return None, []
    _, man = parse(m)
    return _convert(path(d), man) if "original" in man else (man, [])


def _remove(data, man) -> list[str]:
    """Take what `man` recorded out of `data`, only entries equal to the record; the ones that are not there as
    recorded (changed or removed since), described."""
    bad, hooks = [], data.get("hooks")
    for ev, r in man["hooks"].items():
        groups = hooks.get(ev) if isinstance(hooks, dict) else None
        for g in r["groups"]:
            i = next((i for i in reversed(range(len(groups))) if _same(groups[i], g)), None) \
                if isinstance(groups, list) else None
            if i is None:
                bad.append(f"hooks.{ev} has no group equal to the recorded {json.dumps(g, ensure_ascii=False)}")
            else:
                del groups[i]
        if r["created"] and groups == []:
            del hooks[ev]
    if man["hooks_created"] and hooks == {}:
        del data["hooks"]
    if man["statusLine"] is not None:
        if _same(data.get("statusLine"), man["statusLine"]):
            del data["statusLine"]
        else:
            bad.append(f"statusLine is not the recorded {json.dumps(man['statusLine'], ensure_ascii=False)}")
    return bad


def conflicts(root, d) -> list[str]:
    """The entries `d`'s manifest records that its settings.local.json does not hold as recorded ([] without a
    manifest). Reads only; ManifestError / SettingsError when either file cannot be read."""
    _, data = _read(path(d))
    man, _ = load(root, d)
    return _remove(data, man) if man else []


def _strays(p, data) -> list[str]:
    """Notes on what looks like ours in `data` but no manifest recorded: left as it is."""
    return [f"{p}: {c!r} 看起来属于本程序，但不是安装记录的条目：没有删也没有改，不需要时自己删除"
            for c in _commands(data)]


def _plan(root, d) -> tuple[bytes | None, dict, dict, list[str]]:
    """In memory: (the file's bytes, its data and the manifest once this CLI's entries are in place of the recorded
    ones and the hooks not recorded yet are added, notes). The new manifest's statusLine is None when none was
    recorded: install()'s to decide. SettingsError when a recorded entry is not there as recorded (REQ-6)."""
    p, m = path(d), manifest_path(root, d)
    raw, data = _read(p)
    man, notes = load(root, d)
    if man is None:
        man = {"created": raw is None, "hooks_created": "hooks" not in data, "hooks": {}, "statusLine": None}
    elif bad := _remove(data, man):
        raise SettingsError(
            f"{p}: Foremind's entries recorded in {m} are not there as recorded (changed or removed since): "
            f"{'; '.join(bad)}. Nothing was written: Foremind neither takes them over nor adds a second set. Put "
            f"them back as recorded, or remove Foremind's remaining entries from {p} yourself and delete {m}; then "
            "rerun foremind init")
    notes += _strays(p, data)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsError(f"{p}: `hooks` is not an object")
    new = {"format_version": FORMAT_VERSION, "path": str(p), "created": man["created"],
           "hooks_created": man["hooks_created"], "hooks": {}, "statusLine": None}
    for ev, groups in claude.settings()["hooks"].items():
        created = man["hooks"][ev]["created"] if ev in man["hooks"] else ev not in hooks
        if not isinstance(hooks.setdefault(ev, []), list):
            raise SettingsError(f"{p}: hooks.{ev} is not a list")
        hooks[ev].extend(groups)
        new["hooks"][ev] = {"created": created, "groups": groups}
    if man["statusLine"] is not None:  # ours, recorded: this CLI's command in it, its other fields kept
        data["statusLine"] = new["statusLine"] = {**man["statusLine"], "command": claude.foremind_command("statusline")}
    return raw, data, new, notes


def preflight(root, dirs) -> None:
    """Before init writes anything (REQ-6, L385): every manifest in .foremind/install/ (also those of directories no
    longer registered) reads as one this program knows, and each of `dirs` would install without a conflict.
    SettingsError otherwise."""
    mine = {manifest_path(root, d): path(d) for d in dirs}
    for m, r in scan(root):
        if isinstance(r, ManifestError):
            raise SettingsError(f"{r}; nothing was written. " + (
                "Run init with the Foremind that wrote it, or upgrade this one" if r.kind == "future" else
                f"Fix it, or remove Foremind's hooks and status line from {mine.get(m, 'the settings file it is for')} "
                "yourself and delete it; then rerun foremind init (deleting only the manifest makes init add a "
                "second set of hooks)")) from None
    for d in dirs:
        _plan(root, d)


def upgraded(root, d) -> tuple[str, str | None, list[str]] | None:
    """For `foremind upgrade` (m3a.7), in memory: (the manifest text, the settings.local.json text — None: as it
    is — and notes) once `d`'s recorded entries run this CLI; None without a manifest. Nothing is wrapped, no one is
    asked. SettingsError / ManifestError as preflight()."""
    if not manifest_path(root, d).exists():
        return None
    raw, data, new, notes = _plan(root, d)
    text = _dump(data)
    return json.dumps(new, ensure_ascii=False), None if raw is not None and text.encode("utf-8") == raw else text, notes


def install(root, d, consent=None) -> list[str]:
    """Merge our hooks and status line into `d`'s settings.local.json; returns notes for the user.
    `consent(cmd, source)` -> bool is asked before a status line from the project's settings is wrapped
    (None: no one to ask, it is not)."""
    p = path(d)
    raw, data, new, notes = _plan(root, d)
    mine = claude.foremind_command("statusline")
    if new["statusLine"] is None and "statusLine" in data:
        notes.append(f"{p}: 这个目录已有本地状态栏，没有串联；这个目录的会话不经本系统状态栏，读不到额度遥测")
    elif new["statusLine"] is None:  # none recorded: the one in effect is wrapped
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
    atomic_write(manifest_path(root, d), json.dumps(new, ensure_ascii=False))  # before the file: never unrecorded
    text = _dump(data)
    if raw is None or text.encode("utf-8") != raw:
        record(root, p, text)
        atomic_write(p.resolve(), text)
    return notes


def uninstall(root, d) -> list[str]:
    """Only what the manifest recorded goes, entry by entry; without a readable one the file is left as it is.
    Returns notes for the user. A manifest of a newer or illegal format_version raises (install.uninstall stops on it
    before this)."""
    p, m = path(d), manifest_path(root, d)
    raw, data = _read(p)
    broken = None
    try:
        man, notes = load(root, d)
    except ManifestError as e:
        if e.kind != "broken":
            raise
        man, notes, broken = None, [], str(e)
    if man is None:
        if (cmds := _commands(data)) or broken:
            notes.append(f"{broken or f'{m}: 没有安装清单'}：没有改 {p}"
                         + ("；看起来属于本程序的条目请自己删除：" + "、".join(map(repr, cmds)) if cmds else ""))
        return notes
    before = copy.deepcopy(data)
    notes += [f"{p}: {x}: not removed" for x in _remove(data, man)]
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
        text = _dump(data)
        record(root, p, text)
        atomic_write(p.resolve(), text)
    m.unlink(missing_ok=True)
    return notes


def unwrap() -> None:
    """Drop our user-level [statusline] block (call once no enabled project uses it)."""
    with _lock():
        tomlblock.remove(user_config_dir() / "config.toml", STATUSLINE_BLOCK, template=USER_TEMPLATE)


def check(d) -> list[str]:
    """Problems with our entries in `d`'s settings.local.json (doctor): one Foremind hook per event and a status line,
    ours or a local one (kept: local_statusline()). Which Foremind they run is locate()'s."""
    try:
        _, data = _read(path(d))
    except (SettingsError, OSError) as e:
        return [str(e)]
    probs, hooks = [], data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    for ev in claude.settings()["hooks"]:
        got = [h.get("command") for g in hooks.get(ev) or [] if isinstance(g, dict)
               for h in g.get("hooks") or [] if isinstance(h, dict) and ours(h.get("command"))]
        if len(got) != 1 or not got[0].endswith(f" -m foremind hook {ev}"):
            probs.append(f"{path(d)}: hooks.{ev} should hold one Foremind hook for {ev}, found {got}")
    if "statusLine" not in data:
        probs.append(f"{path(d)}: no statusLine: quota telemetry comes through Foremind's")
    return probs


def local_statusline(d) -> str | None:
    """The command of a status line in `d`'s settings.local.json that is not ours (left as it is), else None."""
    sl = _quiet_read(path(d)).get("statusLine")
    if sl is None or isinstance(sl, dict) and ours(sl.get("command")):
        return None
    return str(sl.get("command") if isinstance(sl, dict) else sl)


def wrap_asks(d) -> bool:
    """Whether init wraps `d`'s status line only with the user's consent: none in its settings.local.json, the
    project's `.claude/settings.json` one in effect (install())."""
    return "statusLine" not in _quiet_read(path(d)) and \
        _statusline_in_effect(d)[1] == Path(d) / ".claude" / "settings.json"


# --- which Foremind the hooks run (doctor, m3a.7's checks) ------------------------------------------------------

PROBE = """\
import json, os, sys
r = {"python": sys.version.split()[0], "new_enough": sys.version_info >= (3, 11)}
try:
    import foremind, foremind.cli
except Exception as e:
    r["import"] = "%s: %s" % (type(e).__name__, e)
else:
    r["package"] = os.path.dirname(os.path.abspath(foremind.__file__ or ""))
    r["version"] = getattr(foremind, "__version__", None)
    r["missing"] = []
    for n in getattr(foremind, "TEMPLATES", None) or ["templates"]:
        try:
            foremind.template(n)
        except Exception:
            r["missing"].append(n)
print(json.dumps(r))
"""


def target(cmd) -> tuple[str, str | None, bool] | None:
    """(interpreter, PYTHONPATH or None, whether -P) of a command line in the shape claude.foremind_command() writes,
    or an older one without -P or PYTHONPATH; None for any other line, which is never run."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        return None
    pp = words.pop(0).partition("=")[2] if words and words[0].startswith("PYTHONPATH=") else None
    py, rest = (words[0], words[1:]) if words else ("", [])
    flag = rest[:1] == ["-P"]
    rest = rest[1:] if flag else rest
    if not py or rest[:2] != ["-m", "foremind"] or rest[2:] != ["statusline"] and (
            len(rest) != 4 or rest[2] != "hook"):
        return None
    return py, pp, flag


def _probe(t, cwd) -> dict:
    """Interpreter t[0] with PYTHONPATH t[1] (and -P when t[2]) runs PROBE in `cwd`: a fixed argv, never the line
    itself, and no bytecode written."""
    py, pp, flag = t
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if pp is not None:
        env["PYTHONPATH"] = pp
    try:
        r = subprocess.run([py, *(["-P"] if flag else []), "-c", PROBE], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL, cwd=cwd, env=env)
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return {"absent": True}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": str(e)}
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = (r.stdout + r.stderr).strip()
    if flag and "unknown option" in out.lower() and "-P" in out:  # -P is new in 3.11
        return {"python": "older than 3.11 (it does not know -P)", "new_enough": False}
    return {"error": (out.splitlines() or [f"exit {r.returncode}"])[-1][-300:]}


def expected() -> tuple[str, str, str]:
    """(interpreter, package directory, PYTHONPATH) this CLI writes into hooks."""
    py, pp, _ = target(claude.foremind_command("statusline"))
    return py, os.path.dirname(os.path.abspath(foremind.__file__)), pp


def locate(d) -> list[str]:
    """Why the Foremind commands in `d`'s settings.local.json (recorded or not) do not run this CLI's Foremind
    (REQ-6): an interpreter that is gone, fails, is older than 3.11 or cannot import foremind, a package without its
    templates, or a working one elsewhere (another interpreter, package or PYTHONPATH — one that is gone although the
    import still finds this package elsewhere, say — or no -P). Each distinct interpreter, PYTHONPATH and -P is probed
    once in `d` with a fixed argv; a line of any other shape is reported, not run."""
    try:
        cmds = installed_commands(d)
    except (SettingsError, OSError):
        return []  # check()'s
    py0, pkg0, pp0 = expected()
    probs = [f"{c!r} is not a command line Foremind writes: not run" for c in cmds if target(c) is None]
    for t in dict.fromkeys(t for c in cmds if (t := target(c)) is not None):
        py, pp, flag = t
        r = _probe(t, d)
        where = f"PYTHONPATH {pp}" if pp is not None else "no PYTHONPATH"
        if r.get("absent"):
            probs.append(f"interpreter {py} not found")
        elif "error" in r:
            probs.append(f"interpreter {py} failed: {r['error']}")
        elif not r.get("new_enough"):
            probs.append(f"interpreter {py} is Python {r.get('python')}; Foremind needs 3.11+")
        elif "import" in r:
            probs.append(f"{py} ({where}{'' if flag else ', without -P: a foremind/ in ' + str(d) + ' shadows'}) "
                         f"cannot import foremind: {r['import']}")
        elif r.get("missing") and os.path.realpath(r.get("package", "")) != os.path.realpath(pkg0):
            probs.append(f"the Foremind at {r.get('package')} lacks packaged templates: {', '.join(r['missing'])}")
        elif (py, os.path.realpath(r.get("package", "")), flag) != (py0, os.path.realpath(pkg0), True) \
                or pp is None or os.path.realpath(pp) != os.path.realpath(pp0):
            gone = [e for e in (pp or "").split(os.pathsep) if e and not os.path.isdir(e)]
            probs.append(f"runs Foremind {r.get('version')} at {r.get('package')} with {py}, {where}"
                         + (f" ({', '.join(gone)} gone)" if gone else "") + ("" if flag else " without -P"))
    return probs
