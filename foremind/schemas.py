"""Schemas of every structured artifact (DESIGN §1.4) and a small validator.

A spec is a plain dict:
  type           str | int | bool | list | dict | object (any JSON value)
  opt            field may be absent (default: required)
  min, max       str/list/map length, or int value
  enum           allowed values
  re             full-match regex (str)
  fmt            "datetime" (ISO 8601 with UTC offset) | "date" (YYYY-MM-DD)
  text           string needs at least one non-whitespace character
  items, unique  list element spec; elements must be distinct
  alt            instead of type: a list of specs of different types; the one matching the value's type applies
  fields         object {name: spec}; unknown keys are errors unless extra=True
  tag, variants  tagged object: fields come from variants[obj[tag]]
  keys, values   map (dict without fields): key regex, value spec
  check          cross-field rule, obj -> [(relative_path, message)]; runs only when the object is otherwise valid

`validate(kind, obj)` returns error strings "<path>: <message>" (e.g. "issues[2].severity: ..."); [] = valid.
"""
import re
from datetime import date, datetime

from .state import PENDING_STATES

# --- identifiers -----------------------------------------------------------

_NAME = r"[A-Za-z0-9][A-Za-z0-9_-]*"
PLAN_ID = _NAME  # no ".", so "<plan-id>.<n>" parses unambiguously
REPO_ID = _NAME  # no ":", so "<repo-id>:<path>" parses unambiguously
BATCH_ID = rf"{_NAME}\.[1-9][0-9]*"
QPATH = rf"{_NAME}:(?!/)(?!\.\.(?:/|$))(?!.*/\.\.(?:/|$)).+"  # repo-qualified, relative, no ".." segment
SHA = r"[0-9a-f]{40}|[0-9a-f]{64}"  # full object name (SHA-1 / SHA-256); abbreviations rejected
SHA256 = r"[0-9a-f]{64}"
REQ_ID = r"REQ-[1-9][0-9]*"
Q_ID = r"Q-[1-9][0-9]*"
PV_ID = r"PV-[1-9][0-9]*"
J_ID = r"J-[1-9][0-9]*"
D_ID = rf"{BATCH_ID}\.D[1-9][0-9]*"
F_ID = rf"{BATCH_ID}\.F[1-9][0-9]*"

# --- enums -----------------------------------------------------------------

EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
DIFFICULTIES = ("S", "M", "L")
ORGS = ("single", "exec_review", "controller_seats")  # 单会话 / 执行+审查 / 总控+多席位
REVIEW_LEVELS = ("self", "zero_context", "cross_vendor", "user")  # 自查 / 零上下文 / 跨厂商 / 用户亲验
MODES = ("auto", "watch", "accompany", "user")  # 自动 / 盯着 / 陪同 / 我来做
SEVERITIES = ("must_fix", "should_fix", "note")
ISSUE_STATUSES = ("new", "repeat", "disputed")
AUDIT_SEVERITIES = ("P0", "P1", "P2", "P3")
AUDIT_TRIGGERS = ("l0_fail", "pre_delivery", "plan_amend", "controller_context", "decider",
                  "soft_signals", "interval", "manual", "canary")
PREMISE_KINDS = ("file_hash", "config_key", "manual")
CATALOG_OPS = ("append", "replace", "delete")
CATALOG_FILES = r"index\.md|index/[A-Za-z0-9_-]+\.md|rules\.md|capabilities\.md"  # §20 I22
IMPROVEMENT_CATEGORIES = ("error", "rework", "user_correction", "missed_check",
                          "tool_unused", "tool_misused", "handoff_gap", "planning")


# --- spec helpers ----------------------------------------------------------

def _s(pattern=None):
    return {"type": str, "re": pattern} if pattern else {"type": str, "min": 1, "text": True}


def _enum(*values):
    return {"type": str, "enum": values}


def _list(item, **kw):
    return {"type": list, "items": item, **kw}


def _obj(fields, **kw):
    return {"type": dict, "fields": fields, **kw}


def _opt(spec):
    return {**spec, "opt": True}


_STR = _s()
_INT = {"type": int}
_POS = {"type": int, "min": 1}
_BOOL = {"type": bool}
_CATEGORY = {"type": int, "min": 0, "max": 23}  # 授权表 #0–#23 (§8.1)
_DATETIME = {"type": str, "fmt": "datetime"}
_DATE = {"type": str, "fmt": "date", "re": r"[0-9]{4}-[0-9]{2}-[0-9]{2}"}  # fromisoformat alone accepts 20260401
_HEADS = {"type": dict, "keys": REPO_ID, "values": _s(SHA), "min": 1}
_EFFORT = _enum(*EFFORTS)
# §20 I8 / I45: a start or acceptance command is a string or {run, repo?, expect?}; expect = wanted exit code (default 0)
_COMMAND = {"alt": [_STR, _obj({"run": _STR, "repo": _opt(_s(REPO_ID)), "expect": _opt({"type": int, "min": 0,
                                                                                         "max": 255})})]}


# --- cross-field checks ----------------------------------------------------

def _plan_check(p):
    out = [(f"batches[{i}]", f"must start with plan_id {p['plan_id']!r} + '.'")
           for i, b in enumerate(p["batches"]) if not b.startswith(p["plan_id"] + ".")]
    return out + [(f"revisions[{i}].n", f"expected {i + 1}") for i, r in enumerate(p["revisions"]) if r["n"] != i + 1]


def _batch_check(h):
    out = [] if h["id"].startswith(h["plan_id"] + ".") else [("id", f"must start with plan_id {h['plan_id']!r} + '.'")]
    out += [(f"{f}[{i}].repo", "repo not listed in repos") for f in ("start_commands", "accept_commands")
            for i, c in enumerate(h[f]) if isinstance(c, dict) and c.get("repo", h["repos"][0]) not in h["repos"]]
    return out + [(f"owns_paths[{i}]", "repo not listed in repos")
                  for i, p in enumerate(h["owns_paths"]) if p.split(":", 1)[0] not in h["repos"]]


def _receipt_check(r):
    want = "changes_requested" if any(i["severity"] == "must_fix" for i in r["issues"]) else "approved"
    return [] if r["verdict"] == want else [("verdict", f"must be {want!r} given the issues' severities")]


def _gate_check(g):
    want = "pass" if all(c["ok"] for c in g["checks"]) else "fail"
    return [] if g["verdict"] == want else [("verdict", f"must be {want!r} given the checks")]


def _option_check(field):
    def check(o):
        n = len(o["options"])
        return [] if o.get(field, 1) <= n else [(field, f"option {o[field]} out of range 1..{n}")]
    return check


_NEEDS_ANSWER = ("awaiting_local_confirm", "answered", "applied", "provisional", "confirmed", "overturned",
                 "overdue", "reverted")  # §20 I30; free-text answers go on the event, not in a field (§8.7)


def _pending_check(o):
    out = _option_check("recommended")(o) + _option_check("answer")(o)
    if o.get("state") in _NEEDS_ANSWER and "answer" not in o:
        out.append(("answer", f"required in state {o['state']!r}"))
    return out


def _match_check(m):
    return [] if m else [("", "needs at least one of paths, commands, tools")]


def _patch_check(p):
    out = [] if p["op"] == "append" or p.get("anchor", "").strip() else [("anchor", f"required for {p['op']}")]
    if p["op"] == "delete":
        return out + ([("content", "must be empty for delete")] if p["content"] else [])
    return out + ([] if p["content"].strip() else [("content", f"required for {p['op']}")])


# --- specs -----------------------------------------------------------------

SPECS = {
    # §4.2 plan.md header; §4.2 S9 / §9.2 revisions carry reason, goal hash and approval. approved_at and
    # approved_by record the user's plan approval (program-written, with an event, §20 I1)
    "plan": _obj({
        "plan_id": _s(PLAN_ID),
        "goal_hash": _s(SHA256),
        "approved_at": _opt(_DATETIME),
        "approved_by": _opt(_enum("user")),
        "batches": _list(_s(BATCH_ID), min=1, unique=True),
        "revisions": _list(_obj({
            "n": _POS,
            "at": _DATETIME,
            "reason": _STR,
            "goal_hash": _s(SHA256),
            "approved_by": _enum("user", "controller"),
            "decision": _opt(_s(Q_ID)),
        })),
    }, check=_plan_check),
    # §4.2 5c; also the task config layer (§1.5), so extra config keys are allowed
    "batch_header": _obj({
        "id": _s(BATCH_ID),
        "plan_id": _s(PLAN_ID),
        "reqs": _list(_s(REQ_ID), unique=True),
        "repos": _list(_s(REPO_ID), min=1, unique=True),
        "owns_paths": _list(_s(QPATH), min=1, unique=True),
        "reads": _list(_s(QPATH), unique=True),
        "depends_on": _list(_s(BATCH_ID), unique=True),
        "merge_after": _list(_obj({"batch": _s(BATCH_ID), "reason": _STR})),
        "start_commands": _list(_COMMAND),
        "accept_commands": _list(_COMMAND, min=1),
        "tiers": _obj({
            "difficulty": _enum(*DIFFICULTIES),
            "org": _enum(*ORGS),
            "review": _enum(*REVIEW_LEVELS),
            "model": _STR,
            "effort": _EFFORT,
            "reason": _STR,
        }),
        "mode": _enum(*MODES),
        "hard_block": _list(_CATEGORY, unique=True),
        "budget_estimate": _s(r"[1-9][0-9]*"),  # header scalars are all strings (§20 I21): "80000"
        "must_read": _list(_obj({"path": _STR, "why": _STR})),
        # only the count is capped (§20 I12, decided): a recommendation, not an allowlist, so tool names and
        # steps stay free text; the planner's 2-5 guideline lives in its role card
        "tools": _list(_obj({"name": _STR, "step": _STR}), max=5),
    }, extra=True, check=_batch_check),
    # §6.4 seven fields
    "handoff_section": _obj({
        "goal": _STR,
        "accept_commands": _list(_STR, min=1),
        "state": _obj({
            "repos": {"type": dict, "keys": REPO_ID, "min": 1, "values": _obj({"branch": _STR, "sha": _s(SHA)})},
            "changed_files": _list(_STR),
            "last_test": _opt(_obj({"command": _STR, "exit_code": _INT})),  # absent when nothing was run yet
        }),
        "decisions": _list(_s(D_ID), unique=True),
        "failures": _list(_s(F_ID), unique=True),
        "next": _list(_STR, min=1),  # first item = highest priority
        "unverified": _list(_STR),
        "pointers": _obj({"transcript": _STR, "turns": _list(_STR), "files": _list(_STR)}),
    }),
    # §7.1, §20 I33; the program takes *only* verdict and issues[].{severity, location, summary, disputed} from
    # the reviewer output; every other field (batch, heads, reviewer_session, model, effort, round, scope,
    # rebound_from, and per issue id, fingerprint, status new | repeat) is written by the program. disputed: true
    # becomes status "disputed" and the key is deleted, then the receipt is validated (§20 I9, I25)
    "review_receipt": _obj({
        "batch": _s(BATCH_ID),
        "scope": _enum("full", "delta"),
        "heads": _HEADS,
        "reviewer_session": _STR,
        "model": _STR,
        "effort": _EFFORT,
        "round": _POS,
        "verdict": _enum("approved", "changes_requested"),
        "issues": _list(_obj({
            "id": _STR,
            "fingerprint": _s(r"[0-9a-f]{12,64}"),
            "severity": _enum(*SEVERITIES),
            "status": _enum(*ISSUE_STATUSES),
            "location": _STR,
            "summary": _STR,
        })),
        "rebound_from": _opt(_HEADS),
    }, check=_receipt_check),
    # §7.2
    "acceptance_result": _obj({
        "batch": _s(BATCH_ID),
        "heads": _HEADS,
        "commands": _list(_obj({"command": _STR, "exit_code": _INT, "output_path": _STR}), min=1),
    }),
    # §7.3
    "gate_result": _obj({
        "batch": _s(BATCH_ID),
        "heads": _HEADS,
        "checks": _list(_obj({"name": _STR, "ok": _BOOL, "detail": _opt({"type": str})}), min=1),
        "verdict": _enum("pass", "fail"),
    }, check=_gate_check),
    # §8.6; the whole record (state and answer included) is stored by the program as decisions/<Q-n>.json (§20 I1),
    # the .md next to it is only a rendering for people
    "pending": _obj({
        "id": _s(Q_ID),
        "question": _STR,
        "options": _list(_STR, min=2, max=4, unique=True),
        "recommended": _POS,  # 1-based option number
        "reason": _STR,
        "blocks": _list(_s(BATCH_ID), unique=True),
        "reversible": _BOOL,
        "deadline": _DATETIME,
        "code": _s(r"[A-Za-z0-9]{4,16}"),
        "category": _CATEGORY,
        "answer": _opt(_POS),  # 1-based option number; required by the states in _NEEDS_ANSWER
        "state": _opt(_enum(*PENDING_STATES)),
    }, check=_pending_check),
    # §8.3
    "decision_output": _obj({
        "question": _s(Q_ID),
        "category": _CATEGORY,
        "options": _list(_STR, min=2, max=4),
        "conclusion": _POS,  # 1-based option number
        "confidence": _enum("high", "medium", "low"),
        "facts": _list(_STR, min=1),
        "precedents_cited": _list(_s(J_ID), unique=True),  # may be empty
        "reversible": _BOOL,
    }, check=_option_check("conclusion")),
    # §8.2; valid until the batch is delivered or expires_at, whichever comes first; releases only hits of its
    # own category (§20 I37)
    "exemption": _obj({
        "batch": _s(BATCH_ID),
        "category": _CATEGORY,
        "match": _obj({
            "paths": _opt(_list(_STR, min=1)),
            "commands": _opt(_list(_STR, min=1)),
            "tools": _opt(_list(_STR, min=1)),
        }, check=_match_check),
        "expires_at": _DATETIME,
    }),
    # §8.4
    "provisional": _obj({
        "id": _s(PV_ID),
        "question": _s(Q_ID),
        "batch": _opt(_s(BATCH_ID)),
        "category": _CATEGORY,
        "revert": _STR,
        "deadline": _DATETIME,
        # no state field: the state lives on the Q-n pending record (§20 I24)
    }),
    # §8.5, §20 I32; question = the pending it rests on (answered by the user, or a confirmed, not overturned
    # provisional). id and needs_review are filled by the program, overwriting model output; a non-empty
    # supersedes is not written directly but turned into a user pending
    "precedent": _obj({
        "id": _s(J_ID),
        "question": _s(Q_ID),
        "category": _CATEGORY,
        "conclusion": _STR,
        "premises": _list({"type": dict, "tag": "kind", "variants": {
            "file_hash": {"path": _STR, "sha256": _s(SHA256)},
            "config_key": {"key": _STR, "value": {"type": object}},
            "manual": {"text": _STR},
        }}, min=1),
        "scope": _STR,
        "review_on": _DATE,
        "supersedes": _list(_s(J_ID), unique=True),
        "superseded_by": _opt(_s(J_ID)),
        "needs_review": _BOOL,
    }),
    # §9.3–9.4; the consumer drops findings without evidence *before* validate (so one unproven finding does
    # not void the report); validate itself rejects any finding whose evidence is missing or empty
    "audit_report": _obj({
        "auditor_session": _STR,
        "model": _STR,
        "effort": _EFFORT,
        "trigger": _enum(*AUDIT_TRIGGERS),
        "target": _STR,
        "findings": _list(_obj({
            "severity": _enum(*AUDIT_SEVERITIES),
            "summary": _STR,
            "evidence": _list(_STR, min=1),
            "batches": _list(_s(BATCH_ID), unique=True),
        })),
    }),
    # §1.7; paths relative to .foremind/. anchor is required except for append. replace/delete on rules.md
    # is not applied directly: the program turns it into a user pending (rules.md is user-owned, §20 I22)
    "catalog_patch": _list(_obj({
        "file": _s(CATALOG_FILES),
        "anchor": _opt({"type": str}),
        "op": _enum(*CATALOG_OPS),
        "content": {"type": str},
    }, check=_patch_check)),
    # §1.7 one-shot controller
    "controller_decision": _obj({
        "item": _STR,
        "decision": _STR,
        "plan_amend": _opt(_list(_obj({"batch": _s(BATCH_ID), "changes": {"type": dict, "min": 1}}), min=1)),
        "scope_change": _opt(_obj({"batch": _s(BATCH_ID), "add_owns_paths": _list(_s(QPATH), min=1, unique=True)})),
        "reasons": _list(_STR, min=1),
    }),
    # §11.4
    "improvement_entry": _obj({
        "source": _STR,
        "category": _enum(*IMPROVEMENT_CATEGORIES),
        "description": _STR,
        "evidence": _list(_STR, min=1),
        "premises": _list(_STR),
    }),
}
# §20 I29: the cataloger's whole output; each part is checked by its own spec and written entry by entry.
# Precedents and improvement entries never go through a patch. Consumers call validate("catalog_patch" |
# "precedent" | "improvement_entry", ...) entry by entry instead of judging the whole output at once.
SPECS["catalog_output"] = _obj({
    "patch": SPECS["catalog_patch"],
    "precedents": _list(SPECS["precedent"]),
    "improvements": _list(SPECS["improvement_entry"]),
})
KINDS = tuple(SPECS)


# --- validator -------------------------------------------------------------

_TYPE_NAMES = {str: "string", int: "integer", bool: "boolean", list: "array", dict: "object"}


def _join(path, key):
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path and key else path or key


def _check(spec, v, path, errs):
    def err(msg, at=path):
        errs.append(f"{at or '<root>'}: {msg}")

    if "alt" in spec:
        for a in spec["alt"]:
            if isinstance(v, a["type"]) and not (a["type"] is int and isinstance(v, bool)):
                return _check(a, v, path, errs)
        return err(f"expected {' or '.join(_TYPE_NAMES[a['type']] for a in spec['alt'])}, got {type(v).__name__}")
    t = spec["type"]
    if t is object:
        return
    if not isinstance(v, t) or (t is int and isinstance(v, bool)):
        return err(f"expected {_TYPE_NAMES[t]}, got {type(v).__name__}")
    if "enum" in spec and v not in spec["enum"]:
        return err(f"{v!r} not one of {list(spec['enum'])}")
    before = len(errs)
    if t is not bool:
        n, what = (v, "value") if t is int else (len(v), "length")
        if n < spec.get("min", n):
            err(f"{what} {n} < {spec['min']}")
        if n > spec.get("max", n):
            err(f"{what} {n} > {spec['max']}")
    if t is str:
        if spec.get("text") and v and not v.strip():
            err("blank string")
        if "re" in spec and not re.fullmatch(f"(?:{spec['re']})", v):
            err(f"{v!r} does not match {spec['re']}")
        if spec.get("fmt") == "datetime":
            try:
                if datetime.fromisoformat(v).tzinfo is None:
                    err("datetime needs a UTC offset")
            except ValueError:
                err(f"{v!r} is not an ISO 8601 datetime")
        elif spec.get("fmt") == "date":
            try:
                date.fromisoformat(v)
            except ValueError:
                err(f"{v!r} is not a YYYY-MM-DD date")
    elif t is list:
        for i, x in enumerate(v):
            _check(spec["items"], x, _join(path, i), errs)
        if spec.get("unique") and len(errs) == before and len(set(v)) != len(v):
            err("duplicate items")
    elif t is dict:
        fields = spec.get("fields")
        if "tag" in spec:
            tag = v.get(spec["tag"])
            if not isinstance(tag, str) or tag not in spec["variants"]:
                return err(f"{tag!r} not one of {list(spec['variants'])}", _join(path, spec["tag"]))
            fields = {spec["tag"]: {"type": str}, **spec["variants"][tag]}
        if fields is None:
            for k, x in v.items():
                if "keys" in spec and not (isinstance(k, str) and re.fullmatch(f"(?:{spec['keys']})", k)):
                    err(f"key {k!r} does not match {spec['keys']}")
                if "values" in spec:
                    _check(spec["values"], x, _join(path, k), errs)
        else:
            for k, fs in fields.items():
                if k in v:
                    _check(fs, v[k], _join(path, k), errs)
                elif not fs.get("opt"):
                    err("required", _join(path, k))
            if not spec.get("extra"):
                for k in v.keys() - fields.keys():
                    err("unknown field", _join(path, k))
        if "check" in spec and len(errs) == before:
            for rel, msg in spec["check"](v):
                err(msg, _join(path, rel))


def validate(kind: str, obj) -> list[str]:
    """Errors of `obj` against schema `kind` ([] = valid); unknown kind raises KeyError."""
    errs = []
    _check(SPECS[kind], obj, "", errs)
    return errs


# --- examples (valid; reused by tests and docs) ----------------------------

_SHA1 = "9fceb02d0ae598e95dc970b74767f19372d61af8"
_SHA2 = "1d2c3b4a5f6e7d8c9b0a1f2e3d4c5b6a7f8e9d0c"
_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_HEADS_EX = {"api": _SHA1, "app": _SHA2}

EXAMPLES = {
    "plan": {
        "plan_id": "auth",
        "goal_hash": _HASH,
        "approved_at": "2026-01-05T09:00:00+08:00",
        "approved_by": "user",
        "batches": ["auth.1", "auth.2"],
        "revisions": [{"n": 1, "at": "2026-01-05T09:30:00+08:00", "reason": "auth.2 拆出刷新令牌",
                       "goal_hash": _HASH, "approved_by": "controller"}],
    },
    "batch_header": {
        "id": "auth.2",
        "plan_id": "auth",
        "reqs": ["REQ-1", "REQ-3"],
        "repos": ["api", "app"],
        "owns_paths": ["api:src/auth/token.py", "app:lib/login/*.dart"],
        "reads": ["api:docs/contract/auth.md"],
        "depends_on": ["auth.1"],
        "merge_after": [{"batch": "auth.1", "reason": "auth.1 新增字段，提供方先合"}],
        "start_commands": ["python3 -m unittest discover -s tests"],
        "accept_commands": ["python3 -m unittest tests.test_token"],
        "tiers": {"difficulty": "M", "org": "exec_review", "review": "zero_context",
                  "model": "claude-opus-5-5", "effort": "high", "reason": "两端各改几行，契约已由 auth.1 定"},
        "mode": "auto",
        "hard_block": [18],
        "budget_estimate": "80000",
        "must_read": [{"path": "api:docs/contract/auth.md#token", "why": "字段定义以此为准"}],
        "tools": [{"name": "rg", "step": "查调用点"}, {"name": "gh", "step": "看上游 PR"}],
    },
    "handoff_section": {
        "goal": "登录接口返回刷新令牌，app 端存储并在过期前刷新",
        "accept_commands": ["python3 -m unittest tests.test_token"],
        "state": {
            "repos": {"api": {"branch": "fm/auth.2", "sha": _SHA1}, "app": {"branch": "fm/auth.2", "sha": _SHA2}},
            "changed_files": ["api:src/auth/token.py"],
            "last_test": {"command": "python3 -m unittest tests.test_token", "exit_code": 1},
        },
        "decisions": ["auth.2.D1"],
        "failures": ["auth.2.F1"],
        "next": ["修 test_refresh_expiry 的时区断言", "app 端接入刷新"],
        "unverified": ["待验证：app 端是否已读新字段"],
        "pointers": {"transcript": "~/.claude/projects/api/s1.jsonl", "turns": ["42", "57"],
                     "files": ["api:src/auth/token.py:88"]},
    },
    "review_receipt": {
        "batch": "auth.2",
        "scope": "delta",
        "heads": _HEADS_EX,
        "reviewer_session": "fm-shop-review-3",
        "model": "claude-opus-5-5",
        "effort": "xhigh",
        "round": 2,
        "verdict": "changes_requested",
        "issues": [{"id": "1", "fingerprint": "a1b2c3d4e5f60718", "severity": "must_fix", "status": "repeat",
                    "location": "api:src/auth/token.py:88", "summary": "过期时间用本地时区，跨时区会提前失效"}],
    },
    "acceptance_result": {
        "batch": "auth.2",
        "heads": _HEADS_EX,
        "commands": [{"command": "python3 -m unittest tests.test_token", "exit_code": 0,
                      "output_path": ".foremind/batches/auth.2.accept.out.0.txt"}],
    },
    "gate_result": {
        "batch": "auth.2",
        "heads": _HEADS_EX,
        "checks": [{"name": "receipt", "ok": True}, {"name": "owns_paths", "ok": True, "detail": ""}],
        "verdict": "pass",
    },
    "pending": {
        "id": "Q-12",
        "question": "新增运行时依赖 pyjwt？",
        "options": ["同意", "不同意，用标准库 hmac 实现"],
        "recommended": 2,
        "reason": "只需 HS256，标准库足够",
        "blocks": ["auth.2"],
        "reversible": True,
        "deadline": "2026-01-07T09:30:00+08:00",
        "code": "7KQ2",
        "category": 4,
    },
    "decision_output": {
        "question": "Q-13",
        "category": 3,
        "options": ["加 pytest-timeout 为开发依赖", "不加，用 unittest 自带超时"],
        "conclusion": 2,
        "confidence": "high",
        "facts": ["项目规则：运行时只用标准库", "J-2：测试框架维持 unittest"],
        "precedents_cited": ["J-2"],
        "reversible": True,
    },
    "exemption": {
        "batch": "auth.2",
        "category": 4,
        "match": {"paths": ["api:pyproject.toml"], "commands": ["uv add --dev *"]},
        "expires_at": "2026-01-08T00:00:00+00:00",
    },
    "provisional": {
        "id": "PV-3",
        "question": "Q-13",
        "batch": "auth.2",
        "category": 3,
        "revert": "git revert 提交信息为 'provisional: PV-3' 的提交",
        "deadline": "2026-01-07T09:30:00+08:00",
    },
    "precedent": {
        "id": "J-4",
        "question": "Q-7",
        "category": 10,
        "conclusion": "令牌过期时间一律按 UTC 计算",
        "premises": [
            {"kind": "file_hash", "path": "api:docs/contract/auth.md", "sha256": _HASH},
            {"kind": "config_key", "key": "delivery.api.merge_method", "value": "squash"},
            {"kind": "manual", "text": "客户端不展示过期时间"},
        ],
        "scope": "api 与 app 的认证相关批次",
        "review_on": "2026-04-01",
        "supersedes": ["J-1"],
        "needs_review": False,
    },
    "audit_report": {
        "auditor_session": "fm-shop-audit-1",
        "model": "claude-opus-5-5",
        "effort": "xhigh",
        "trigger": "pre_delivery",
        "target": "fm-shop-controller-2",
        "findings": [{"severity": "P2", "summary": "总控驳回 must_fix #1 未附理由",
                      "evidence": ["events.jsonl#review_rejected auth.2 r2", "auth.2.log.md:131"],
                      "batches": ["auth.2"]}],
    },
    "catalog_patch": [
        {"file": "index.md", "anchor": "## 契约", "op": "append",
         "content": "认证令牌字段 → api:docs/contract/auth.md#token → 改登录或刷新逻辑前 → auth.2"},
        {"file": "rules.md", "anchor": "- 过期时间用本地时区（已被 J-4 取代）", "op": "delete", "content": ""},
    ],
    "controller_decision": {
        "item": "Q-15",
        "decision": "批准 auth.2 扩大到 api:src/auth/session.py",
        "scope_change": {"batch": "auth.2", "add_owns_paths": ["api:src/auth/session.py"]},
        "reasons": ["session.py 只被 auth.2 使用，与在跑批次不相交"],
    },
    "improvement_entry": {
        "source": "auth.2",
        "category": "missed_check",
        "description": "新增开发依赖未走待决，审查才发现",
        "evidence": ["auth.2 review r1 issue 3"],
        "premises": ["依赖清单与运行时依赖在同一 pyproject.toml"],
    },
}
EXAMPLES["catalog_output"] = {
    "patch": EXAMPLES["catalog_patch"][:1],
    "precedents": [EXAMPLES["precedent"]],
    "improvements": [EXAMPLES["improvement_entry"]],
}
