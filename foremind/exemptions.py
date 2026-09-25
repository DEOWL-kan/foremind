"""Exemptions `.foremind/exemptions/<Q-n>.json` (schema `exemption`; DESIGN §8.2, §20 I7 I8 I37).

Program-written after a pending is approved. Valid for its batch and category until the batch is delivered or
`expires_at`, whichever comes first (never counted per use): approving a #3 never releases a #22 or #7 hit, however
wide its globs. Globs are fnmatch: `paths` against a path's repo-qualified
(`<repo>:<path>`) or absolute form, `commands` against one command segment, `tools` against the tool name.
Files that fail the schema are ignored, never trusted.
"""
import json
from datetime import datetime, timezone
from fnmatch import fnmatchcase

from foremind.paths import state_dir
from foremind.schemas import validate
from foremind.state import BATCH_SIDE

DONE = ("delivered", "merged", "cataloged", "cancelled")


def batch_done(header: dict) -> bool:
    """Delivered or later, also while parked on a side branch entered from there (state_prior, §20 I1)."""
    state = header.get("state")
    return state in DONE or (state in BATCH_SIDE and header.get("state_prior") in DONE)


def find(root, batch, kind, values, *, category, header=None, now=None) -> str | None:
    """Id (Q-n) of a valid exemption of `batch` and `category` whose `kind` globs match one of `values`, else None."""
    if not batch or batch_done(header or {}):
        return None
    now = now or datetime.now(timezone.utc)
    for p in sorted((state_dir(root) / "exemptions").glob("*.json")):
        try:
            ex = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (validate("exemption", ex) or ex["batch"] != batch or ex["category"] != category
                or datetime.fromisoformat(ex["expires_at"]) <= now):
            continue
        if any(fnmatchcase(v, g) for g in ex["match"].get(kind, ()) for v in values):
            return p.stem
    return None
