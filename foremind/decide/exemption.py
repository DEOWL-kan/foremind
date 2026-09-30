"""Handler of approved `hit` pendings (pending.py contract): issue `.foremind/exemptions/<Q-n>.json` (schema
`exemption`, DESIGN §8.2, §20 I7 I37) for the request's batch and the record's category.

The match is narrowed to exactly what was hit or asked for, glob-escaped so no wildcard in it widens it: paths only
in repo-qualified form (`<repo>:<path>`, the absolute form of a hit is dropped), matched by exemptions.py through
pathmatch.owns (normalized, casefolded on a case-insensitive volume, and one ending in `/` covers everything below
it); commands as the one hit segment verbatim, compared by fnmatch with nothing left to expand (never
`npm install left-*`, which would also release `left-pad evil`); tools by exact name. Valid until the batch is
delivered (exemptions.batch_done) or expires_at, whichever comes first.
"""
import glob
import json
import re
from datetime import datetime, timezone, timedelta

from foremind.fsutil import atomic_write
from foremind.paths import state_dir
from foremind.schemas import QPATH, validate

EXPIRES = timedelta(days=7)  # ponytail: fixed; delivery usually ends it first. Make it a config key if 7 days bites.


def narrow(match: dict) -> dict:
    paths = [p for p in match.get("paths", ()) if isinstance(p, str) and re.fullmatch(QPATH, p)]
    out = {k: list(dict.fromkeys(glob.escape(v) for v in vs if isinstance(v, str) and v))
           for k, vs in (("paths", paths), ("commands", match.get("commands", ())), ("tools", match.get("tools", ())))}
    return {k: v for k, v in out.items() if v}


def apply_answer(root, record: dict, request: dict) -> str:
    ex = {"batch": request.get("batch"), "category": record["category"], "match": narrow(request.get("match") or {}),
          "expires_at": (datetime.now(timezone.utc) + EXPIRES).isoformat(timespec="seconds")}
    if not ex["match"]:
        raise ValueError("没有可签发的精确匹配（路径只签 <仓库>:<路径> 形式，命中在所有仓库之外）")
    if errs := validate("exemption", ex):
        raise ValueError("凭据不合 schema，未签发：" + "; ".join(errs))
    atomic_write(state_dir(root) / "exemptions" / f"{record['id']}.json",
                 json.dumps(ex, ensure_ascii=False, indent=1) + "\n")
    return (f"已签发放行凭据 exemptions/{record['id']}.json（批次 {ex['batch']}、#{ex['category']}，"
            f"本批交付前或 {ex['expires_at'][:10]} 前有效）")
