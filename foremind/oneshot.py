"""Start and read one-shot read-only roles other than the reviewer (DESIGN §1.7, §20 I47): the decider and the one-shot
controller. prepare() writes the materials and returns what a supervisor phase hands to `t.start_job`; nothing here
starts a process.

.foremind/oneshots/<session>/ holds the materials and prompt.md (the role card, then the materials by absolute path:
the job runs in the project root, not here). The role only prints one JSON object; output() finds it in the job's
stdout.
"""
import json
import re
import uuid
from fnmatch import fnmatchcase
from pathlib import Path

from foremind.defaults import TABLE
from foremind.paths import state_dir
from foremind.review import READ_ONLY_TOOLS

_ROLES = Path(__file__).resolve().parent.parent / "templates" / "roles"


def route(cfg, role) -> tuple[str, str]:
    """Model and effort of `role` (routes.<role>.*); exclusions are never bypassed (as review.route, DESIGN §1.5)."""
    # ponytail: Claude only, as review.route
    model = cfg.get(f"routes.{role}.model", "claude-opus-5-5")
    effort = cfg.get(f"routes.{role}.effort", "xhigh")
    if "claude" in cfg.get("exclude.providers", TABLE["exclude.providers"]):
        raise ValueError(f"{role} vendor claude is excluded")
    for pat in cfg.get("exclude.models", TABLE["exclude.models"]):
        if any(fnmatchcase(name.lower(), pat.lower()) for name in (model, f"claude:{model}")):
            raise ValueError(f"{role} model {model} is excluded by {pat!r}")
    return model, effort


def prepare(root, role, cfg, materials: dict) -> tuple[str, list, dict]:
    """(session, argv, env). `materials` {file name: text, or data written as sorted JSON}."""
    model, effort = route(cfg, role)
    session = str(uuid.uuid4())
    mat = state_dir(root) / "oneshots" / session
    mat.mkdir(parents=True)
    lines = ["## 材料（只读，只读这些）"]
    for name, data in materials.items():
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        (mat / name).write_text(text, encoding="utf-8")
        lines.append(f"- {mat / name}")
    card = (_ROLES / f"{role}.md").read_text(encoding="utf-8")
    (mat / "prompt.md").write_text(card + "\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    dynamic_off = cfg.get("oneshot.exclude_dynamic_prompt", TABLE["oneshot.exclude_dynamic_prompt"]) is True
    argv = ["claude", "-p", "--restricted", "--tools", READ_ONLY_TOOLS, "--permission-mode", "plan",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--model", model, "--effort", effort, "--output-format", "json", "--session-id", session,
            *(["--exclude-dynamic-system-prompt-sections"] if dynamic_off else []),
            "--", f"Read {mat / 'prompt.md'} and follow it. Output only the JSON object it asks for."]
    return session, argv, {"FOREMIND_SESSION": session, "FOREMIND_ROLE": role}


def output(root, jid, key) -> dict:
    """The role's JSON object (the first one with field `key`) from job `jid`'s stdout: `claude -p --output-format
    json` or bare text, as review.parse_output. ValueError when there is none; OSError when stdout is unreadable."""
    raw = (state_dir(root) / "jobs" / jid / "stdout.log").read_text(encoding="utf-8", errors="replace")
    try:
        obj = json.loads(raw)
    except ValueError:
        obj = None
    if isinstance(obj, dict) and key in obj:
        return obj
    if isinstance(obj, dict) and isinstance(obj.get("result"), str):
        if obj.get("is_error"):
            raise ValueError(f"the role reported an error: {obj['result'][:200]}")
        raw = obj["result"]
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", raw):  # braces in the prose around it must not break parsing
        try:
            found = dec.raw_decode(raw, m.start())[0]
        except ValueError:
            continue
        if isinstance(found, dict) and key in found:
            return found
    raise ValueError(f"no JSON object with {key!r} in the output")
