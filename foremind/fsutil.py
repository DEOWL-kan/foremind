"""Atomic writes, O_APPEND lines, flock locks, hashing (DESIGN §1.1)."""
import contextlib
import fcntl
import hashlib
import os
import tempfile
from pathlib import Path

from foremind.paths import state_dir, user_config_dir


class LockBusy(Exception):
    pass


def atomic_write(path, data: str | bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        with contextlib.suppress(FileNotFoundError):
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)  # keep the replaced file's mode
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)  # make the rename itself durable
        finally:
            os.close(dfd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def append_line(path, line: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (line if line.endswith("\n") else line + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        while data:
            data = data[os.write(fd, data):]
    finally:
        os.close(fd)


@contextlib.contextmanager
def file_lock(path, *, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise LockBusy(str(path)) from None
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the flock


def project_lock(root, *, blocking=True):
    return file_lock(state_dir(root) / "state.lock", blocking=blocking)


def global_lock(*, blocking=False):
    return file_lock(user_config_dir() / "supervisor.lock", blocking=blocking)


def sha256_bytes(b) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()
