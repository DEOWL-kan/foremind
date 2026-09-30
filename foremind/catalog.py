"""Write the cataloger's output (schema `catalog_output`) into `.foremind/` (DESIGN §1.7, §6.2, §20 I22 I29 I32).

The cataloger only prints; apply() checks each entry of the three parts on its own and writes it, so one bad entry
rejects only itself. Every write is an atomic_write of the whole file under the project state lock.

Patch (`catalog_patch`, files index.md, index/<part>.md, rules.md, capabilities.md):
  anchor   must equal one whole line of the file (trailing CR/LF aside), exactly once; missing or repeated: rejected
  append   no anchor: at the end of the file. With an anchor: at the end of the section that starts at the anchor
           line, i.e. before the next heading of the same or a higher level (any heading when the anchor is not a
           heading), and before that section's trailing blank lines
  replace  the anchor line becomes `content`;  delete: the anchor line is removed. A heading anchor stands for its
           whole section (templates/roles/cataloger.md): replace swaps the section's text (trailing blank lines
           kept), delete removes it up to the next heading of the same or a higher level
  rules.md replace/delete may loosen a rule: not written, a user pending (kind catalog, #22) holds the patch instead,
           its question naming the real scope (a line or a whole section) and request.original quoting the text
           that would go (ORIGINAL_LINES at most); request.original_sha256 hashes all of it, and approval applies
           only while that text is unchanged (a replace/delete request without it is refused: fail-closed)
Precedent (`precedent`): id (J-<highest + 1>) and needs_review (false) are the program's. Its question must be a Q-n
  answered by the user or a confirmed provisional; every J-n it supersedes must exist and not be superseded already
  (checked again on approval). With supersedes non-empty it becomes a user pending (the precedent's own category); on
  approval it is written and each superseded J-n gets superseded_by. precedents.md holds one block per precedent:
  "## J-n", a blank line, then a ```json fence holding an object (anything else: _precedents raises ValueError).
  mark_review sets needs_review (supervisor/phases/provisional.py, a machine-checked premise that no longer holds).
Improvement (`improvement_entry`): appended to improvements.md as "## <UTC date> <category>" plus a ```json fence.

Event catalog_applied {by, question?, results: [{part, index, result: applied|pending|rejected, note, ...}],
files: {path under .foremind/: sha256}} (the hashes let L0 be checked later). apply_answer is the pending.py handler
for kind catalog: the request carries `patch` or `precedent`.
"""
import json
import re
import sys
from datetime import datetime, timezone

from foremind import config, lock, repos
from foremind import defaults as defaults_mod
from foremind.decide import pending, table
from foremind.events import EventLog
from foremind.fsutil import atomic_write, sha256_bytes, sha256_file
from foremind.paths import state_dir
from foremind.schemas import validate

PARTS = (("patch", "catalog_patch"), ("precedents", "precedent"), ("improvements", "improvement_entry"))
RULES_CATEGORY = 22
OPTIONS = ["应用这条修改", "不应用"]
# only so a precedent validates before its J-n exists; never stored in a pending request (the number is given on write)
PLACEHOLDERS = {"id": "J-1", "needs_review": False}
ORIGINAL_LINES = 40  # rules.md text a replace/delete pending quotes (request.original), truncated beyond this
_HEADING = re.compile(r"(#{1,6})\s")
# ponytail: the program is the only writer of precedents.md, so a regex over its own format is enough
_UNKNOWN = object()
_BLOCK = re.compile(r"^## (J-[1-9][0-9]*)\n\n```json\n(.*?)\n```\n", re.M | re.S)


def _log(root):
    return EventLog(state_dir(root) / "events.jsonl")


def _level(line) -> int:
    # ponytail: a "# " line inside a code fence also counts as a heading; track fences if index files grow them
    m = _HEADING.match(line)
    return len(m.group(1)) if m else 7


def _patched(text, p, taken=None) -> str:
    """`text` with patch `p` applied; ValueError when its anchor does not match exactly one whole line. The lines a
    replace or delete removes are appended to the list `taken`, if given."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    body = p["content"] if p["content"].endswith("\n") or not p["content"] else p["content"] + "\n"
    if p["op"] == "append" and not p.get("anchor"):
        return "".join(lines) + body
    hits = [i for i, ln in enumerate(lines) if ln.rstrip("\r\n") == p["anchor"]]
    if len(hits) != 1:
        raise ValueError(f"anchor 在 {p['file']} 里{'找不到' if not hits else f'出现 {len(hits)} 处'}：{p['anchor']!r}")
    i, head = hits[0], _level(lines[hits[0]]) <= 6
    nxt = i + 1  # the section: up to the next heading of the same or a higher level (any heading if not a heading)
    while nxt < len(lines) and _level(lines[nxt]) > min(_level(lines[i]), 6):
        nxt += 1
    end = nxt  # the section's text, without its trailing blank lines
    while end > i + 1 and not lines[end - 1].strip():
        end -= 1
    if p["op"] == "append":
        lines[end:end] = [body]
    else:  # a heading anchor: the whole section (templates/roles/cataloger.md)
        stop = (end if p["op"] == "replace" else nxt) if head else i + 1
        if taken is not None:
            taken += lines[i:stop]
        lines[i:stop] = [body] if p["op"] == "replace" else []
    return "".join(lines)


def _write(root, rel, edit) -> None:
    """Read .foremind/<rel> ("" when missing), atomic_write edit(text) back, under the state lock."""
    path = state_dir(root) / rel
    with lock.state_lock(root):
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        atomic_write(path, edit(text))


def _precedents(root) -> dict:
    """{J-n: precedent} of precedents.md; ValueError when a block is not a JSON object (m2b.3 r3: nothing on it can be
    checked, and every reader would trip over it)."""
    path = state_dir(root) / "precedents.md"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    out = {m.group(1): json.loads(m.group(2)) for m in _BLOCK.finditer(text)}
    if bad := [j for j, p in out.items() if not isinstance(p, dict)]:
        raise ValueError(f"判例 {bad[0]} 不是 JSON 对象")
    return out


def _block(prec) -> str:
    return f"## {prec['id']}\n\n```json\n{json.dumps(prec, ensure_ascii=False, indent=1)}\n```\n"


def _set(text, j, **fields) -> str:
    """precedents.md `text` with J-n `j`'s block rewritten with `fields`."""
    m = next((m for m in _BLOCK.finditer(text) if m.group(1) == j), None)
    if m is None:
        raise ValueError(f"precedents.md 里没有 {j}")
    return text.replace(m.group(0), _block({**json.loads(m.group(2)), **fields}))


def mark_review(root, j) -> None:
    _write(root, "precedents.md", lambda text: _set(text, j, needs_review=True))


def holds(root, cfg, premise) -> bool:
    """Whether a machine-checked premise still holds: file_hash (sha256 of the file at `path`, `<repo-id>:<path>`
    under that repo of [[repos]] or a bare path under the project root; missing, unreadable or an unknown repo: not)
    or config_key (the value in the merged config `cfg`; a key set in no layer compares what it reads as, _unset);
    manual is the cataloger's (True)."""
    if premise["kind"] == "file_hash":
        try:
            return sha256_file(_file(root, cfg, premise["path"])) == premise["sha256"]
        except (OSError, ValueError, config.ConfigError):
            return False
    if premise["kind"] == "config_key":
        if cfg is None:  # unreadable: whether the user set the key is unknown, so no default stands in
            return False
        if (k := premise["key"]) in cfg:
            return cfg[k] == premise["value"]
        return (v := _unset(root, cfg, k)) is not _UNKNOWN and v == premise["value"]  # REQ-16: set nowhere, its default
    return True


def _unset(root, cfg, k):
    """What key `k`, set in no layer of `cfg`, reads as; _UNKNOWN when that is not known. A per-repo level or ci falls
    back to delivery.level or gate.ci as review.repo_cfg does; a target_branch is the repo's default branch as
    review.target_branch reads it (unknown when [[repos]] has none, or does not load); other per-repo keys go by
    defaults()'s `delivery.repo.*.<key>`."""
    if m := re.fullmatch(r"delivery\.repo\.([^.]+)\.(level|ci|target_branch)", k):  # repo ids have no dot (repos._ID)
        if m[2] == "target_branch":
            try:
                return next((r.default_branch for r in repos.load_repos(root, cfg) if r.id == m[1]), None) or _UNKNOWN
            except (OSError, ValueError, config.ConfigError):
                return _UNKNOWN
        if (k := {"level": "delivery.level", "ci": "gate.ci"}[m[2]]) in cfg:
            return cfg[k]
    return defaults().get(re.sub(r"^delivery\.repo\.[^.]+\.", "delivery.repo.*.", k), _UNKNOWN)


def defaults() -> dict:
    """foremind/defaults.py's table (m2d.9: moved there)."""
    return defaults_mod.table()


def _file(root, cfg, path):
    if ":" not in path:
        return root / path
    rid, rel = repos.split_qualified(path, known := repos.load_repos(root, cfg or {}))
    return next(r.path for r in known if r.id == rid) / rel


def _check_precedent(root, prec) -> None:
    qid = prec["question"]
    st = pending.load(root, qid).get("state", "open")
    if st in ("answered", "applied"):
        by = [e.get("by") for e in _log(root).iter()
              if e["type"] == "pending_answered" and e.get("question") == qid]
        if by[-1:] != ["user"]:
            raise ValueError(f"{qid} 不是用户答复的（{by[-1] if by else '找不到答复事件'}）")
    elif st != "confirmed":
        raise ValueError(f"{qid} 当前是 {st}：判例只能来自用户已答复的待决或已确认的暂定决定")
    precs = _precedents(root) if prec["supersedes"] else {}
    if missing := [j for j in prec["supersedes"] if j not in precs]:
        raise ValueError(f"要取代的判例不存在：{'、'.join(missing)}")
    if gone := [f"{j}（已被 {precs[j]['superseded_by']} 取代）" for j in prec["supersedes"]
                if precs[j].get("superseded_by")]:  # m2a.10 r3
        raise ValueError(f"不能取代已被取代的判例：{'、'.join(gone)}")


def _write_precedent(root, prec) -> str:
    """Append `prec` with the next J-n and mark what it supersedes; returns the J-n."""
    out = {}

    def edit(text):
        found = {m.group(1) for m in _BLOCK.finditer(text)}
        prec.update(id=f"J-{1 + max((int(j[2:]) for j in found), default=0)}", needs_review=False)
        for j in prec["supersedes"]:
            if j in found:
                text = _set(text, j, superseded_by=prec["id"])
        out["id"] = prec["id"]
        return text + ("\n" if text and not text.endswith("\n\n") else "") + _block(prec)

    _write(root, "precedents.md", edit)
    return out["id"]


def _ask(root, cfg, by, *, question, category, reason, payload) -> str:
    """A user pending for one entry (deduplicated by its content while unresolved); returns the Q-n."""
    key = "catalog:" + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    req = {"kind": "catalog", "category": category, "approve_option": 1, "key": key, "by": by, **payload}
    rec, created = pending.create(root, cfg, question=question, options=OPTIONS, recommended=2, reason=reason,
                                  category=category, blocks=[], reversible=category not in table.LOCKED, request=req)
    if created:
        try:
            pending.notify_later(root, rec["id"])
        except OSError as e:  # the Q-n stands; `decide --notify` can push it later
            print(f"foremind catalog: {rec['id']} 没能安排推送：{e}", file=sys.stderr)
    return rec["id"]


def _cfg(root) -> dict:
    try:
        return config.load(root)
    except Exception:  # noqa: BLE001 — as `decide`: an unreadable config reads as the strictest preset
        return {"authz.preset": "conservative"}


def _one(root, cfg, by, part, entry, written) -> dict:
    if part == "patch":
        if entry["file"] == "rules.md" and entry["op"] != "append":
            path = state_dir(root) / "rules.md"
            taken = []  # a bad anchor: rejected now, before any pending
            _patched(path.read_text(encoding="utf-8") if path.exists() else "", entry, taken)
            verb = "替换" if entry["op"] == "replace" else "删除"
            what = f"的整个小节（{len(taken)} 行）" if _level(taken[0]) <= 6 else "的一行"
            original = "".join(taken[:ORIGINAL_LINES]) + (
                f"…（共 {len(taken)} 行，只附前 {ORIGINAL_LINES} 行）" if len(taken) > ORIGINAL_LINES else "")
            q = _ask(root, cfg, by, question=f"编目员要{verb} rules.md {what}：{entry['anchor']}",
                     category=RULES_CATEGORY, payload={"patch": entry, "original": original,
                                                           "original_sha256": sha256_bytes("".join(taken).encode())},
                     reason="rules.md 归用户，替换或删除可能放宽规则；看过请求里的补丁与原文（original）再批准")
            return {"result": "pending", "question": q, "note": f"rules.md 的{entry['op']}转为用户待决 {q}"}
        _write(root, entry["file"], lambda t: _patched(t, entry))
        written.add(entry["file"])
        return {"result": "applied", "note": f"{entry['file']} {entry['op']}"}
    if part == "precedents":
        prec = entry
        _check_precedent(root, prec)
        if prec["supersedes"]:
            q = _ask(root, cfg, by, question=f"编目员要用新判例取代 {'、'.join(prec['supersedes'])}：{prec['conclusion']}",
                     category=prec["category"], payload={"precedent": {k: v for k, v in prec.items() if k not in PLACEHOLDERS}},
                     reason="取代判例会改变以后同类事项的默认结论；看过请求里的判例再批准")
            return {"result": "pending", "question": q, "note": f"取代判例转为用户待决 {q}"}
        jid = _write_precedent(root, prec)
        written.add("precedents.md")
        return {"result": "applied", "note": jid}
    day = datetime.now(timezone.utc).date().isoformat()
    _write(root, "improvements.md", lambda t: t + ("\n" if t and not t.endswith("\n\n") else "") +
           f"## {day} {entry['category']}\n\n```json\n{json.dumps(entry, ensure_ascii=False, indent=1)}\n```\n")
    written.add("improvements.md")
    return {"result": "applied", "note": "improvements.md"}


def _event(root, by, results, written, **extra) -> None:
    files = {rel: sha256_file(state_dir(root) / rel) for rel in sorted(written)}
    _log(root).append("catalog_applied", by=by, results=results, files=files, **extra)


def apply(root, output, *, by) -> list[dict]:
    """Check and write each entry; returns one result per entry (also in the catalog_applied event)."""
    if not isinstance(output, dict):
        raise ValueError(f"编目员输出应是对象，收到 {type(output).__name__}")
    cfg, results, written = _cfg(root), [], set()
    for key in output.keys() - {k for k, _ in PARTS}:
        results.append({"part": key, "index": None, "result": "rejected", "note": "不认识的部分"})
    for part, kind in PARTS:
        entries = output.get(part, [])
        if not isinstance(entries, list):
            results.append({"part": part, "index": None, "result": "rejected", "note": "应是数组"})
            continue
        for i, entry in enumerate(entries):
            if part == "precedents" and isinstance(entry, dict):  # id, needs_review, superseded_by: the program's
                entry = {**{k: v for k, v in entry.items() if k != "superseded_by"}, **PLACEHOLDERS}
            try:
                if errs := validate(kind, [entry] if part == "patch" else entry):
                    raise ValueError("; ".join(errs))
                res = _one(root, cfg, by, part, entry, written)
            except (ValueError, OSError) as e:  # one entry fails alone; UnicodeDecodeError is a ValueError
                res = {"result": "rejected", "note": f"{type(e).__name__}: {e}" if isinstance(e, OSError) else str(e)}
            results.append({"part": part, "index": i, **res})
    _event(root, by, results, written)
    return results


def apply_answer(root, record: dict, request: dict) -> str:
    """pending.py handler: the user approved a catalog pending; write the patch or precedent it holds."""
    written = set()
    if patch := request.get("patch"):
        if errs := validate("catalog_patch", [patch]):
            raise ValueError("补丁不合 schema：" + "; ".join(errs))
        if patch["op"] != "append" and not request.get("original_sha256"):  # m2a.10 r6: fail-closed
            raise ValueError(f"{patch['file']} 的{patch['op']}请求里没有原文哈希（original_sha256），无从核对，未应用")

        def edit(text):  # under the lock: what goes must still be what the user saw (request.original_sha256)
            taken = []
            out = _patched(text, patch, taken)
            if patch["op"] != "append" and sha256_bytes("".join(taken).encode()) != request["original_sha256"]:
                raise ValueError(f"{patch['file']} 里要改动的原文在建待决后变了，未应用；请重新编目")
            return out

        _write(root, patch["file"], edit)
        written.add(patch["file"])
        note = f"已改 {patch['file']}（{patch['op']}）"
    elif prec := request.get("precedent"):
        prec = {**prec, **PLACEHOLDERS}
        if errs := validate("precedent", prec):
            raise ValueError("判例不合 schema：" + "; ".join(errs))
        _check_precedent(root, prec)  # the question may have been overturned since
        note = f"已写判例 {_write_precedent(root, dict(prec))}，取代 {'、'.join(prec['supersedes'])}"
        written.add("precedents.md")
    else:
        raise ValueError("请求里没有补丁或判例")
    _event(root, "user", [{"part": "patch" if patch else "precedents", "index": None,
                                                "result": "applied", "note": note}], written, question=record["id"])
    return note
