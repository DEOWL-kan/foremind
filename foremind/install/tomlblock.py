"""Marked blocks in files we do not own (DESIGN §13.1): `# >>> foremind <name> >>>` … `# <<< foremind <name> <<<`.

tomllib only reads, so an existing file only ever gets a block appended; uninstall deletes it by its markers. Every
write is preceded by a backup (user config dir `backups/`, never inside a repo) and, for TOML, followed by a tomllib
parse of the whole file; a failed parse puts the old bytes back. A file that did not end with a newline gets one
before the block, and the block then ends without one, so removal restores the file byte for byte.
A new file is `template` plus the block; removing the block from it deletes the file when only the template is left.
Also used for `.git/info/exclude` (check=False: not TOML).
"""
import json
import re
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from foremind.fsutil import atomic_write, sha256_bytes
from foremind.paths import user_config_dir


BACKUPS_KEPT = 10  # per file: older backups of the same path are deleted


class BlockError(Exception):
    pass


def markers(name) -> tuple[str, str]:
    return f"# >>> foremind {name} >>>", f"# <<< foremind {name} <<<"


def _span(text, name) -> tuple[int, int] | None:
    begin, end = markers(name)
    if text.startswith(begin + "\n"):
        i = 0
    elif (k := text.find("\n" + begin + "\n")) >= 0:
        i = k + 1
    else:
        return None
    j = text.find("\n" + end, i)
    if j < 0:
        raise BlockError(f"block {name!r}: begin marker without end marker")
    j += 1 + len(end)
    if j == len(text):
        return max(i - 1, 0), j  # block at EOF without a newline: the newline before it was ours too
    if text[j] != "\n":
        raise BlockError(f"block {name!r}: text after the end marker on its line")
    return i, j + 1


def strip(text, name) -> str:
    s = _span(text, name)
    return text if s is None else text[:s[0]] + text[s[1]:]


def get(text, name) -> str | None:
    """The block's body, or None."""
    s = _span(text, name)
    if s is None:
        return None
    lines = text[s[0]:s[1]].strip("\n").split("\n")
    return "\n".join(lines[1:-1])


def insert(text, name, body) -> str:
    """Replace the block if present, else append it."""
    begin, end = markers(name)
    text = strip(text, name)
    block = f"{begin}\n{body.strip(chr(10))}\n{end}"
    return text + block + "\n" if not text or text.endswith("\n") else text + "\n" + block


def value(v) -> str:
    """TOML literal for a str, bool, int or list of them (JSON string escapes are valid TOML basic strings; JSON leaves
    U+007F raw, TOML forbids it)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(v, list):
        return "[" + ", ".join(map(value, v)) + "]"
    raise TypeError(f"no TOML literal for {v!r}")


def _backup(path, old: bytes):
    """Keeps the newest BACKUPS_KEPT of this path's backups (the fixed-width stamp sorts by time); other files stay."""
    d, prefix = user_config_dir() / "backups", f"{path.name}.{sha256_bytes(str(path).encode())[:8]}."
    atomic_write(d / f"{prefix}{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.bak", old)
    try:
        ours = sorted(p for p in d.iterdir()
                      if p.name.startswith(prefix) and re.fullmatch(r"\d{8}-\d{6}-\d{6}\.bak", p.name[len(prefix):]))
        for p in ours[:-BACKUPS_KEPT]:
            p.unlink(missing_ok=True)
    except OSError as e:  # a failed cleanup must not stop the write this backup is for
        print(f"foremind: 旧备份没有删掉（{e}）", file=sys.stderr)


def _write(path, old: bytes | None, new: str | None, check, before=None):
    if before:
        before(path, new)
    if old is not None:
        _backup(path, old)
    if new is None:
        path.unlink()
        return
    atomic_write(path, new)
    if not check:
        return
    try:
        tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        if old is None:
            path.unlink()
        else:
            atomic_write(path, old)
        raise BlockError(f"{path}: not valid TOML with the foremind block, restored the previous file: {e}") from None


def _decode(path, old: bytes, check) -> str:
    try:
        text = old.decode("utf-8")
    except UnicodeDecodeError as e:
        raise BlockError(f"{path}: not UTF-8 ({e}); convert it to UTF-8, then rerun") from None
    if check and text.startswith("\ufeff"):
        raise BlockError(f"{path}: starts with a byte order mark, which TOML does not allow; remove it, then rerun")
    return text


def add(path, name, body, *, template="", check=True, before=None) -> None:
    """`before(path, new text)` is called before the file is written (settings.record)."""
    path = Path(path).resolve()  # a symlink (dotfiles) stays a link; its target gets the block
    old = path.read_bytes() if path.exists() else None
    text = template if old is None else _decode(path, old, check)
    new = insert(text, name, body)
    if old is None or new.encode("utf-8") != old:
        _write(path, old, new, check, before)


def remove(path, name, *, template=None, check=True, dry_run=False, before=None) -> bool:
    """Delete the block; True if there was one. The file goes when nothing but `template` is left. `dry_run`: only
    raise what removing would."""
    path = Path(path).resolve()
    if not path.exists():
        return False
    old = path.read_bytes()
    text = _decode(path, old, check)
    new = strip(text, name)
    if new == text or dry_run:
        return new != text
    _write(path, old, None if template is not None and new == template else new, check, before)
    return True
