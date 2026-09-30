"""Claude Code seat launch (DESIGN §1.1, M1-0 items 1 and 5).

Hooks reach the seat through a per-session `--settings` file (they merge with the user's hooks and inherit the
session's environment); `--permission-mode` is always explicit; `--bare` is never used (it skips hooks).
Multi-repo batches add each worktree with `--add-dir` and set CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1,
without which the added directories' CLAUDE.md is not loaded.
"""
import json
import shlex
import sys
from pathlib import Path

from foremind.fsutil import atomic_write
from foremind.vendors import Launch

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "Notification", "StopFailure")
# Seats run in acceptEdits (seat.permission_mode): these skip the prompt for read-only and test commands. No push,
# install or rm; left out too, since a flag lets them delete or run commands: find (-delete/-exec), rg (--pre),
# git branch (-D); Grep and Glob cover search. git diff/log/show stay (REQ-1 names git diff) although `--output=<file>`
# writes a file (m2a.5 denies that in the guard): `python3 -m unittest` already runs whatever test the seat wrote, so
# the hard limits rest on the PreToolUse guard (a deny still wins over allow), not on this list.
ALLOW = ("Read", "Grep", "Glob", *(f"Bash({c}:*)" for c in (
    "git status", "git diff", "git log", "git show", "git rev-parse", "git ls-files", "git add",
    "git commit", "ls", "cat", "head", "tail", "wc", "grep", "pwd", "python3 -m unittest",
    "foremind log", "foremind handoff", "foremind review", "foremind decide", "foremind status")))
FOREMIND_SKIPS = ("log", "handoff", "review", "decide", "status")  # also as foremind_command() spells them (REQ-12)
_PKG_PARENT = Path(__file__).resolve().parents[2]


def foremind_command(*args) -> str:
    """Shell line running `foremind <args>` with this interpreter. PYTHONPATH on that process only: a source checkout
    works from any cwd without leaking into the agent's shell. `-P` (§20 I53①): the cwd is not put on sys.path, so a
    `foremind/` in the session's cwd (a worktree of foremind itself) cannot stand in for the installed package."""
    return f"PYTHONPATH={shlex.quote(str(_PKG_PARENT))} {shlex.join([sys.executable, '-P', '-m', 'foremind', *args])}"


def hook_command(event) -> str:
    return foremind_command("hook", event)


def settings(role=None) -> dict:
    """The planner commits nothing (its draft goes in through plan submit): git add/commit not among its skips."""
    allow = [r for r in ALLOW if role != "planner" or r not in ("Bash(git add:*)", "Bash(git commit:*)")]
    # ponytail: whether a prefix rule matches past the leading `PYTHONPATH=…` is not tried yet; the plain rules stay
    allow += [f"Bash({foremind_command(c)}:*)" for c in FOREMIND_SKIPS]
    return {"permissions": {"allow": allow},
            "hooks": {e: [{"matcher": "*" if e.endswith("ToolUse") else "",
                           "hooks": [{"type": "command", "command": hook_command(e)}]}] for e in HOOK_EVENTS}}


def launch(*, session, batch, role, project_root, settings_path, cwd, permission_mode, add_dirs=(),
           multi_repo=False, model=None, effort=None, env=None) -> Launch:
    """settings_path None: no --settings (a session the project's installed hooks already cover, the controller);
    `env` replaces the seat's FOREMIND_* variables (the controller has no FOREMIND_SESSION)."""
    argv = ["claude"]
    if settings_path is not None:
        atomic_write(settings_path, json.dumps(settings(role), indent=2) + "\n")
        argv += ["--settings", str(settings_path)]
    argv += ["--permission-mode", permission_mode]
    for d in add_dirs:
        argv += ["--add-dir", str(d)]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    env = dict(env) if env is not None else {"FOREMIND_SESSION": session, "FOREMIND_BATCH": batch,
                                             "FOREMIND_ROLE": role, "FOREMIND_PROJECT": str(project_root)}
    if multi_repo:
        env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] = "1"
    return Launch(argv, env, Path(cwd))
