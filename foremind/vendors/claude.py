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

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
_PKG_PARENT = Path(__file__).resolve().parents[2]


def foremind_command(*args) -> str:
    """Shell line running `foremind <args>` with this interpreter. PYTHONPATH on that process only: a source checkout
    works from any cwd without leaking into the agent's shell. `-P` (§20 I53①): the cwd is not put on sys.path, so a
    `foremind/` in the session's cwd (a worktree of foremind itself) cannot stand in for the installed package."""
    return f"PYTHONPATH={shlex.quote(str(_PKG_PARENT))} {shlex.join([sys.executable, '-P', '-m', 'foremind', *args])}"


def hook_command(event) -> str:
    return foremind_command("hook", event)


def settings() -> dict:
    return {"hooks": {e: [{"matcher": "*" if e.endswith("ToolUse") else "",
                           "hooks": [{"type": "command", "command": hook_command(e)}]}] for e in HOOK_EVENTS}}


def launch(*, session, batch, role, project_root, settings_path, cwd, permission_mode, add_dirs=(),
           multi_repo=False, model=None, effort=None) -> Launch:
    atomic_write(settings_path, json.dumps(settings(), indent=2) + "\n")
    argv = ["claude", "--settings", str(settings_path), "--permission-mode", permission_mode]
    for d in add_dirs:
        argv += ["--add-dir", str(d)]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    env = {"FOREMIND_SESSION": session, "FOREMIND_BATCH": batch, "FOREMIND_ROLE": role,
           "FOREMIND_PROJECT": str(project_root)}
    if multi_repo:
        env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] = "1"
    return Launch(argv, env, Path(cwd))
