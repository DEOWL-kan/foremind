"""`foremind catalog apply <file.json>`: write a cataloger's output (schema `catalog_output`) into .foremind/
(foremind/catalog.py). The user (no Foremind session), the controller and the supervisor may run it; seats, the
planner and every other role may not. Exit 1 when any entry was rejected (the others are written all the same).
"""
import json
import os
import sys
from pathlib import Path

from foremind import catalog
from foremind.paths import ProjectNotFound, find_project_root

APPLIERS = ("controller", "supervisor")  # plus the user: no FOREMIND_ROLE and no FOREMIND_SESSION


def register(sub):
    p = sub.add_parser("catalog", help="write a cataloger's output into .foremind/")
    p.add_argument("action", choices=["apply"])
    p.add_argument("file", help="the cataloger's output, JSON (schema catalog_output)")
    p.set_defaults(func=_run)


def _run(a):
    role, session = os.environ.get("FOREMIND_ROLE") or None, os.environ.get("FOREMIND_SESSION") or None
    if role not in APPLIERS and (role or session):
        print(f"foremind catalog: 只有用户、controller、supervisor 能落盘编目（当前角色 {role or '未标'}）", file=sys.stderr)
        return 1
    try:
        root = find_project_root()
        output = json.loads(Path(a.file).read_text(encoding="utf-8"))
        results = catalog.apply(root, output, by=role or "user")
    except (OSError, ValueError, ProjectNotFound) as e:
        print(f"foremind catalog: {e}", file=sys.stderr)
        return 1
    for r in results:
        print(f"{r['part']}[{'-' if r['index'] is None else r['index']}] {r['result']}：{r['note']}")
    return 1 if any(r["result"] == "rejected" for r in results) else 0
