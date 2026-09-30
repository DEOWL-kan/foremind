"""`foremind statusline`: records quota/context telemetry, then runs the user's own status line command and prints its
output unchanged (DESIGN §1.1: wrap, never replace). The wrapped command is `[statusline] command` in the *user*
config only — a project config must not get to run commands on every status refresh."""
import contextlib
import os
import signal
import subprocess
import sys
import tomllib

from foremind import telemetry
from foremind.paths import user_config_dir

TIMEOUT_S = 10


def register(sub):
    sub.add_parser("statusline").set_defaults(func=_run)  # internal: set as Claude Code's statusLine command


def wrapped_command() -> str | None:
    try:
        with open(user_config_dir() / "config.toml", "rb") as f:
            cmd = tomllib.load(f).get("statusline", {}).get("command")
    except (OSError, ValueError, AttributeError):  # ValueError: TOMLDecodeError, UnicodeDecodeError
        return None
    return cmd if isinstance(cmd, str) and cmd.strip() else None


def run_wrapped(cmd, raw: bytes) -> bytes | None:
    """The command's stdout; None on failure or timeout. It runs in its own process group (not a new session: it keeps
    the terminal, e.g. for `stty size </dev/tty`) and a timeout kills the group: killing only the shell leaves a child
    that holds the pipe, and reading it would block until that exits."""
    try:
        with subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, process_group=0) as p:
            try:
                return p.communicate(raw, timeout=TIMEOUT_S)[0]
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):  # the group is gone already
                    os.killpg(p.pid, signal.SIGKILL)
                return None
    except OSError:
        return None


def _run(args):
    raw = sys.stdin.buffer.read()
    telemetry.record_statusline(raw)
    if (cmd := wrapped_command()) and (out := run_wrapped(cmd, raw)) is not None:
        sys.stdout.buffer.write(out)
        sys.stdout.flush()
    return 0
