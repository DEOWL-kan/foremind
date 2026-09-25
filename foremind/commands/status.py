"""`foremind status [--all]` (DESIGN §13.5): batches by state, sessions (heartbeat, busy_tool), open pendings, quota
state (M1-7's .foremind/quota.json). `--all`: every project in the user-level registry."""
import json
import re
import sys
from collections import Counter
from pathlib import Path

from foremind import header as hdr
from foremind import install
from foremind.paths import ProjectNotFound, find_project_root, state_dir
from foremind.schemas import BATCH_ID
from foremind.supervisor.ready import OPEN_Q


def register(sub):
    p = sub.add_parser("status", help="batches, sessions, pendings and quota of this project")
    p.add_argument("--all", action="store_true", help="every enabled project (~/.config/foremind/projects)")
    p.set_defaults(func=_run)


def _json(p):
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def report(root) -> str:
    sd, out = state_dir(root), [f"项目 {root}"]
    if not sd.is_dir():
        return out[0] + "\n  没有 .foremind/（未 init 或已删除）"
    batches = []
    for p in sorted((sd / "batches").glob("*.md")):
        if re.fullmatch(BATCH_ID, p.stem):
            try:
                h, _ = hdr.parse(p.read_text(encoding="utf-8"))
            except (OSError, hdr.HeaderError):
                h = {"state": "（批次头读不了）"}
            batches.append((p.stem, h))
    counts = Counter(h.get("state", "planned") for _, h in batches)
    out.append("批次：" + (" · ".join(f"{s} {n}" for s, n in sorted(counts.items())) or "无"))
    for bid, h in batches:
        extra = f"（{h['blocked_reason']}）" if h.get("blocked_reason") else ""
        out.append(f"  {bid:<16} {h.get('state', 'planned')}{extra}  {h.get('title', '')}".rstrip())
    beats = [hb for p in sorted((sd / "heartbeats").glob("*.json")) if (hb := _json(p))]
    out.append(f"会话：{len(beats) or '无'}")
    for hb in beats:
        busy = " busy_tool" if hb.get("tool_open") else ""
        out.append(f"  {hb.get('session')}  批次 {hb.get('batch') or '-'}  心跳 {hb.get('ts')}{busy}")
    qs = [q for p in (sd / "decisions").glob("Q-*.json") if (q := _json(p))]
    out.append(f"未决：{sum(q.get('state', 'open') in OPEN_Q for q in qs)}")
    groups = (_json(sd / "quota.json") or {}).get("groups") or {}
    out.append("额度：" + (" · ".join(f"{g} {st.get('state')}" for g, st in sorted(groups.items())
                                     if isinstance(st, dict)) or "unknown（监督进程还没跑过）"))
    return "\n".join(out)


def _run(args):
    if args.all:
        try:
            roots = [Path(p) for p in install.registered()]
        except install.InstallError as e:  # e.g. a registry that is not UTF-8
            print(f"foremind status: {e}", file=sys.stderr)
            return 1
        if not roots:
            print(f"没有已启用的项目（{install.registry_path()}）")
        print("\n\n".join(report(r) for r in roots))
        return 0
    try:
        print(report(find_project_root()))
    except ProjectNotFound as e:
        print(f"foremind status: {e}", file=sys.stderr)
        return 1
    return 0
