"""`foremind statusline`: records quota/context telemetry, then runs the user's own status line command and prints its
output unchanged (DESIGN §1.1: wrap, never replace). The wrapped command is `[statusline] command` in the *user*
config only — a project config must not get to run commands on every status refresh."""
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


def _run(args):
    raw = sys.stdin.buffer.read()
    telemetry.record_statusline(raw)
    if cmd := wrapped_command():
        try:
            out = subprocess.run(cmd, shell=True, input=raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=TIMEOUT_S).stdout
        except (OSError, subprocess.TimeoutExpired):
            return 0
        sys.stdout.buffer.write(out)
        sys.stdout.flush()
    return 0
