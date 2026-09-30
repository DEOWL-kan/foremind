import json
import os
import re
import sys
from pathlib import Path

from foremind import batchlog, schemas
from foremind.paths import ProjectNotFound, find_project_root

FORMAT = ("D/F 记录格式（templates/PROTOCOL.md 第 10 条）：\n"
          "  {b}.D<n> · 选了什么 · 没选什么 · 为什么 · #k · 证据指针\n"
          "  {b}.F<n> · 做法 → 现象 → 原因判断 → 证据指针")
NOTES = re.compile(r"^## 交付说明.*$", re.M)
_JSON_BLOCK = re.compile(r"^```json[ \t]*\n(.*?)\n```", re.S | re.M)


def register(sub):
    p = sub.add_parser("log", help="append a section to a batch log (text from --file or stdin)")
    p.add_argument("batch_id")
    p.add_argument("--author", default=os.environ.get("FOREMIND_SESSION") or "user")
    p.add_argument("--file")
    p.set_defaults(func=_run)


def bad_records(text, batch) -> list[str]:
    """Lines that start a D or F record of `batch` (optionally as a list item) and miss its shape: a D needs five
    non-empty `·` fields after the id, the fourth `#k`; an F needs three `→`."""
    rx = re.compile(rf"^[ \t]*(?:[-*][ \t]+)?{re.escape(batch)}\.([DF])[1-9][0-9]*(?![0-9])(.*)$", re.M)
    bad = []
    for m in rx.finditer(text):
        if m[1] == "D":
            f = [x.strip() for x in m[2].split("·")]
            ok = len(f) == 6 and f[0] == "" and all(f[1:]) and re.fullmatch(r"(?:授权表类别 ?)?#[0-9]+", f[4])
        else:
            ok = m[2].lstrip().startswith("·") and m[2].count("→") == 3
        if not ok:
            bad.append(m[0].strip())
    return bad


def delivery_notes(text) -> list[tuple]:
    """(notes, errors) for each `## 交付说明` heading in text (REQ-9): the first ```json block after it, checked
    against schema delivery_notes; notes is None when there is no block or it is not JSON."""
    out = []
    for m in NOTES.finditer(text):
        if not (b := _JSON_BLOCK.search(text, m.end())):
            out.append((None, ["no ```json block after the heading"]))
            continue
        try:
            obj = json.loads(b[1])
        except json.JSONDecodeError as e:
            out.append((None, [f"not JSON: {e}"]))
            continue
        out.append((obj, schemas.validate("delivery_notes", obj)))
    return out


def _run(args):
    try:
        text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
        if not text.strip():
            raise ValueError("empty text")
        if bad := bad_records(text, args.batch_id):
            raise ValueError("格式不对，未追加：\n" + "\n".join(f"  {x}" for x in bad) + "\n"
                             + FORMAT.format(b=args.batch_id))
        if bad := [e for _, errs in delivery_notes(text) for e in errs]:
            raise ValueError("交付说明不合格（schema delivery_notes），未追加：\n" + "\n".join(f"  {x}" for x in bad))
        print(batchlog.append(find_project_root(), args.batch_id, text, author=args.author))
    except (OSError, ProjectNotFound, ValueError, batchlog.LogRewritten) as e:
        print(f"foremind log: {e}", file=sys.stderr)
        return 1
    return 0
