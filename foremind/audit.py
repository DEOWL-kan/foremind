"""L0 fact checks (DESIGN §9.2) and the L1 auditor's plumbing (§9.3–9.4), shared by the supervisor phase
(supervisor/phases/audit.py) and `foremind audit` (commands/audit.py). Nothing here calls a model or writes an event.

L0 hard failures, each {check, target, fingerprint, detail}; the fingerprint names the fact, so the same fact is
reported once and a `resumed` after its report accepts it (§20 I51: the user looked):
  config       a #22 file at its fixed place under the project root or a repo root (CONFIG_GLOBS; a trailing /*
               takes the whole tree, as guard.SYSTEM_GLOBS; keyed by that place, a symlink not resolved, its content
               read through it) differs from its baseline (`l0_baseline {path, sha256}`, sha256 null: absent) without
               an approval written after that baseline: the latest user_config_edit, program_config_write (init,
               uninstall) or l0_config_accepted of the place, or of what it resolves to, names this hash, or the
               report of this very change (the
               fingerprint binds the baseline event and the hash) was followed by `resumed`. A root's first
               reconciliation (`l0_root {path}` once) takes what is there as baselines; a file appearing there later
               needs an approval too. A settings file of a seat (sessions/<s>.settings.json) is judged by the
               settings_sha256 of its session's seat_launch results after its baseline (m2b.10), first seen too;
               without that field any launch record of the session (seat_launch result, planner_opened) takes its
               first-seen content, as does the content vendors.claude.launch writes (a planner that never got its
               heartbeat records nothing), and with neither it waits while younger than a launch's heartbeat wait.
               Approved: a new baseline. Carries sha256
  goal         a plan's goal.md body hash, the sha256 in its header and plan.md's goal_hash differ, or differ from
               the goal_hash of the plan's latest goal_frozen / plan_approved / plan_amended (goal.md gone then too);
               a hash that is not a string matches nothing; a plan with such a goal_hash that does not load (a header
               broken, plan.md or its directory gone) fails unless its goal.md is intact (plan freeze comes before
               plan.md)
  batch_log    batchlog.verify fails (rewritten outside `foremind log`), or the log cannot be read: every batch with a
               batches/<id>.log.md or a batch_log_appended record, whether its plan loads or not
  event_chain  EventLog.verify_chain finds a problem in events.jsonl; fingerprinted with the lines it names
  seat_ancestor  an event claiming the user whose seat_ancestor names a session (events.seat_ancestor, REQ-11 ④,
               m2c.6): a seat's process wrote it; target the event id. "unknown" is no failure (supervisor/phases/
               bounds.py tells it)
Each fingerprint carries the current content, so a place accepted once and tampered with again is reported again,
and the id of its item's (check, target) latest `l0_cleared`: an item reported and now passing is cleared (the
tick writes `l0_cleared {check, target}`), so the same fact again after it was put right is a new one.
  l0_error     the checks themselves raised: nothing was checked, so it fails closed (the tick ends the pass)
Files are read before the events, so an approval written before a file was read is seen.
"""
import glob
import json
import os
import re
import time
from fnmatch import fnmatchcase
from pathlib import Path

from foremind import batchlog, config, handoff, repos, seat
from foremind.events import EventLog
from foremind.fsutil import project_lock, sha256_bytes
from foremind.paths import state_dir
from foremind.plan import model
from foremind.review import heads_hash
from foremind.schemas import validate
from foremind.vendors import claude

CONFIG_GLOBS = ("foremind.toml", ".foremind/config.toml", ".foremind/roles/*", ".foremind/hooks/*",
                ".foremind/adapters/*", ".foremind/sessions/*.settings.json", ".claude/settings*.json",
                "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".codex/*")
SETTINGS_SUFFIX = ".settings.json"
HOLDS = ("P0", "P1")  # findings that keep a batch in awaiting_audit until `foremind audit --release`
MAX_BYTES = 90_000  # the auditor's materials, ≈30k tokens at ~3 UTF-8 bytes a token (§9.3)


def fingerprint(*parts) -> str:
    return sha256_bytes("\0".join(str(p) for p in parts).encode())[:16]


# --- L0 ------------------------------------------------------------------------------------------

def _dirs(root, cfg) -> list[str]:
    out = {os.path.realpath(root)}
    try:
        out |= {os.path.realpath(r.path) for r in repos.load_repos(root, cfg)}
    except config.ConfigError:
        pass  # the tick reports the config
    return sorted(out)


def config_files(dirs) -> dict:
    """{place: sha256} of the #22 files present at their fixed places under `dirs`: a symlink is its own place
    (a link made later is a new file, one removed a deletion), its content that of its target."""
    out = {}
    for d in dirs:
        for g in CONFIG_GLOBS:
            for p in Path(d).glob(g[:-1] + "**/*" if g.endswith("/*") else g):
                try:
                    if p.is_file():
                        out[str(p)] = sha256_bytes(p.read_bytes())
                except OSError:
                    continue  # gone meanwhile, or unreadable: an absent baseline file is still a change
    return out


def _session_of(path) -> str | None:
    p = Path(path)
    return p.name[:-len(SETTINGS_SUFFIX)] if p.parent.name == "sessions" and p.name.endswith(SETTINGS_SUFFIX) \
        else None


def _history(evs):
    """{baseline path: sha}, {approved path: sha}, reconciled roots, {session: settings shas its launches recorded},
    sessions with a launch record, reported fps, accepted fps, reported fps neither accepted nor followed by a
    `paused`, {plan: goal_hash last frozen or approved}, {baseline path: id of its baseline event},
    {(check, target): id of its latest l0_cleared}, (check, target) reported since that, batches with a
    batch_log_appended. A baseline uses up the approvals and launch records before it: they approve the change after
    it only."""
    h = {"base": {}, "approved": {}, "roots": set(), "launched": {}, "started": set(), "reported": set(),
         "accepted": set(), "unpaused": set(), "goals": {}, "base_id": {}, "gen": {}, "open": set(), "logs": set()}
    since = set()
    for e in evs:
        ty = e["type"]
        if ty == "l0_baseline":
            h["base"][e.get("path")], h["base_id"][e.get("path")] = e.get("sha256"), e.get("id")
            h["approved"].pop(e.get("path"), None)
            h["launched"].pop(_session_of(e.get("path") or ""), None)
        elif ty == "l0_root":
            h["roots"].add(e.get("path"))
        elif ty == "batch_log_appended":
            h["logs"].add(e.get("batch"))
        elif ty in ("user_config_edit", "program_config_write"):  # the latter: init / uninstall (settings.record)
            h["approved"][e.get("path")] = e.get("sha256")
        elif ty == "l0_config_accepted":
            for f in e.get("files") or []:
                if isinstance(f, dict):
                    h["approved"][f.get("path")] = f.get("sha256")
        elif (ty == "seat_launch" and e["phase"] == "result") or ty == "planner_opened":
            h["started"].add(e.get("session"))
            if e.get("settings_sha256"):
                h["launched"].setdefault(e.get("session"), set()).add(e["settings_sha256"])
        elif ty in ("goal_frozen", "plan_approved", "plan_amended") and e.get("goal_hash"):
            h["goals"][e.get("plan")] = e["goal_hash"]
        elif ty == "l0_hard_failure" and e.get("fingerprint"):
            h["reported"].add(e["fingerprint"])
            h["unpaused"].add(e["fingerprint"])
            since.add(e["fingerprint"])
            h["open"].add((e.get("check"), e.get("target")))
        elif ty == "l0_cleared":
            h["gen"][(e.get("check"), e.get("target"))] = e.get("id")
            h["open"].discard((e.get("check"), e.get("target")))
        elif ty == "paused":
            h["unpaused"] = set()
        elif ty == "resumed":
            h["accepted"] |= since
            since, h["unpaused"] = set(), set()
    return h


def reported(evs) -> set:
    return _history(evs)["reported"]


def unpaused(evs) -> set:
    """Reported, not accepted, no `paused` after: the pass that reported them did not pause (§9.2: pause again)."""
    return _history(evs)["unpaused"]


TICK_CHECKS = ("tick_crash", "config_invalid", "events_invalid", "l0_error")  # what tick._invalid reports


def uncleared(evs) -> list:
    """The latest report {check, target, fingerprint, detail} of each (check, target) of TICK_CHECKS no l0_cleared
    followed: no pass got through L0 since, and l0() does not check them itself (r9)."""
    last = {(e.get("check"), e.get("target")): e for e in evs
            if e["type"] == "l0_hard_failure" and e.get("check") in TICK_CHECKS}
    live = _history(evs)["open"]
    return [{k: e.get(k) for k in ("check", "target", "fingerprint", "detail")} for ct, e in last.items() if ct in live]


def generation(evs, check, target):
    """What a fingerprint of (check, target) carries: the id of its latest l0_cleared, None before any."""
    return _history(evs)["gen"].get((check, target))


def _young(path, grace) -> bool:
    try:
        return 0 <= time.time() - os.stat(path).st_mtime < grace
    except OSError:
        return False


def _hash(x):
    """A hash as a header holds it: a list or object is no hash, it matches nothing (and must not raise)."""
    return x if x is None or isinstance(x, str) else repr(x)


def l0(root, cfg) -> tuple[list, list, list, list]:
    """(roots reconciled for the first time, baselines [(path, sha256)] to record, hard failures, (check, target)
    reported and now passing, to clear) as of now; failures accepted by a `resumed` after their report are left out
    (config ones become baselines). Whatever the checks raise is one l0_error failure, accepted or not: nothing was
    checked (§9.2 fails closed)."""
    try:
        return _l0(root, cfg)
    except Exception as e:
        what = f"{type(e).__name__}: {e}"
        try:  # the same error after the checks ran again is a new one
            with EventLog(state_dir(root) / "events.jsonl")._lock():
                gen = generation(handoff.history(root), "l0_error", "l0")
        except Exception:
            gen = None  # the history is what fails
        return [], [], [{"check": "l0_error", "target": "l0", "fingerprint": fingerprint("l0_error", "l0", gen, what),
                         "detail": what[:300]}], []


def _l0(root, cfg):
    dirs = _dirs(root, cfg)
    cur = config_files(dirs)  # before the events: see the module doc
    new_base, fails = [], []
    events = EventLog(state_dir(root) / "events.jsonl")

    def fp(check, target, *facts):  # the fact, since its item last passed
        return fingerprint(check, target, h["gen"].get((check, target)), *facts)

    # plan amend / freeze write goal.md, plan.md and their event under the project lock; hooks append events under
    # the events lock only: a line half appended is no history, no batch log record, no broken chain
    with project_lock(root), events._lock():
        evs = list(handoff.history(root))
        h = _history(evs)
        for pid in sorted(set(model.plan_ids(root)) | h["goals"].keys()):  # a frozen plan gone is one too
            bound = _hash(h["goals"].get(pid))  # none: the plan never froze a goal (the test fixtures write none)
            try:
                plan = model.load(root, pid)
            except (ValueError, OSError) as e:  # PlanError, HeaderError; the tick reports the plan
                d = state_dir(root) / "plans" / pid
                try:  # plan freeze records the goal before plan.md is written: an intact goal.md is no failure
                    g = model.read(d / "goal.md")
                    intact = model.text_hash(g.body) == bound == _hash(g.header.get("sha256"))
                except (ValueError, OSError):
                    intact = False
                if bound is not None and not intact:  # a frozen goal nothing vouches for any more
                    now = [sha256_bytes(f.read_bytes()) if f.is_file() else None
                           for f in (d / "goal.md", d / "plan.md")]
                    fails.append({"check": "goal", "target": pid, "detail": f"plan unreadable ({type(e).__name__})",
                                  "fingerprint": fp("goal", pid, bound, type(e).__name__, *now)})
                continue
            trio = (None, None) if plan.goal is None else (model.text_hash(plan.goal.body),
                                                           _hash(plan.goal.header.get("sha256")))
            trio += (_hash(plan.doc.header.get("goal_hash")),)
            if (plan.goal is not None or bound is not None) and len(set(trio) | ({bound} if bound else set())) > 1:
                fails.append({"check": "goal", "target": pid, "fingerprint": fp("goal", pid, *trio, bound),
                              "detail": "goal.md body {} · goal.md sha256 {} · plan.md goal_hash {} · bound {}".format(
                                  *(str(x)[:12] for x in (*trio, bound)))})
        bdir = state_dir(root) / "batches"  # every log there or recorded, whether a plan loads or lists it or not
        for bid in sorted({p.name[:-len(".log.md")] for p in bdir.glob("*.log.md")}
                          | {b for b in h["logs"] if isinstance(b, str)}):
            try:
                if batchlog.verify(root, bid):
                    continue
                log = bdir / f"{bid}.log.md"
                now, why = sha256_bytes(log.read_bytes()) if log.is_file() else None, "changed outside foremind log"
            except (OSError, ValueError) as e:  # unreadable, a directory, a broken archive: nothing vouches for it
                now = why = f"unreadable ({type(e).__name__})"
            fails.append({"check": "batch_log", "target": bid, "fingerprint": fp("batch_log", bid, now),
                          "detail": why})
        for e in evs:
            if isinstance(s := e.get("seat_ancestor"), str) and s != "unknown":
                fails.append({"check": "seat_ancestor", "target": e["id"], "fingerprint": fp("seat_ancestor", e["id"]),
                              "detail": f"{e['type']} claims the user but was written under session {s}"})
        probs = events.verify_chain()
        lines = dict(events._lines()) if probs else {}
    if probs:  # each with the content of the line it names: that line tampered again is another fact
        at = [lines.get(int(m[1]), "") if (m := re.match(r"line (\d+):", p)) else "" for p in probs]
        fails.append({"check": "event_chain", "target": "events.jsonl", "detail": "; ".join(probs[:3]),
                      "fingerprint": fp("event_chain", "events.jsonl", *(f"{p} {sha256_bytes(a.encode())}"
                                                                           for p, a in zip(probs, at)))})
    base, approved = h["base"], h["approved"]
    places = {d: [os.path.join(glob.escape(d), g) for g in CONFIG_GLOBS] for d in dirs}
    gone = {p for p, s in base.items() if s is not None and p not in cur
            and any(fnmatchcase(p, g) for ps in places.values() for g in ps)}
    grace = max(seat.setting(cfg, k) for k in ("seat.sessionstart_timeout_s", "seat.manual_sessionstart_timeout_s"))
    written = {sha256_bytes((json.dumps(claude.settings(r), indent=2) + "\n").encode())  # as claude.launch writes it
               for r in (None, "planner")}  # the planner's lacks git add/commit
    for p in sorted(cur.keys() | gone):
        now, s = cur.get(p), _session_of(p)
        if p in base and base[p] == now:
            continue
        f = fp("config", p, h["base_id"].get(p, "new"), now)  # this change from this very baseline
        first = p not in base and not any(d in h["roots"] and any(fnmatchcase(p, g) for g in ps)
                                          for d, ps in places.items())
        if s and s in h["launched"]:  # the settings its launch started with (m2b.10), first seen too
            ok = now in h["launched"][s]
        else:
            ok = first or (p not in base and bool(s) and (s in h["started"] or now in written))
            if not ok and s and p not in base and _young(p, grace + 60):
                continue  # a launch that has not recorded itself yet (a planner's only after its heartbeat)
        if ok or any(k in approved and approved[k] == now for k in (p, os.path.realpath(p))) \
                or f in h["accepted"]:  # user_config_edit names the file a link resolves to
            new_base.append((p, now))
        else:
            fails.append({"check": "config", "target": p, "fingerprint": f, "sha256": now,
                          "detail": "deleted" if now is None else "new without approval" if p not in base
                          else "changed without approval"})
    return ([d for d in dirs if d not in h["roots"]], new_base,
            [x for x in fails if x["fingerprint"] not in h["accepted"]],
            sorted(h["open"] - {(x["check"], x["target"]) for x in fails}, key=str))


# --- L1 ------------------------------------------------------------------------------------------

def pre_key(evs, bid) -> str:
    """Dedupe key of `bid`'s pre-delivery audit: the heads it entered awaiting_audit with (the gate's batch_state)."""
    hd = next((e.get("heads") for e in reversed(evs) if e["type"] == "batch_state" and e.get("batch") == bid
               and e.get("state") == "awaiting_audit"), None)
    return f"pre_delivery:{bid}:{heads_hash(hd or {})}"


def held(evs, bid) -> bool:
    """`bid`'s pre-delivery audit is held (P0/P1, or no valid report): its awaiting_audit waits on the user
    (tick.waits); until then the auditor is due."""
    key = pre_key(evs, bid)
    return any(e["type"] == "audit_held" and e.get("key") == key for e in evs)


def decider_key(e) -> str:
    """Dedupe key of the sampled decision `e` (decider_decided): its content."""
    return "decider:{}:{}".format(e.get("question"), fingerprint(*(e.get(k) for k in (
        "question", "category", "conclusion", "confidence"))))


_OPEN = re.compile(r"[\[{]")


def _as_findings(x):
    if isinstance(x, dict) and "findings" in x:
        x = x["findings"]
    return x if isinstance(x, list) and all(isinstance(f, dict) for f in x) else None


def findings(raw: str) -> list:
    """The auditor's findings list from its stdout (`claude -p --output-format json` or bare text), failing closed
    (it releases a batch): the whole answer, `[]` included; else the one non-empty top-level JSON array of objects
    (or object with `findings`) in prose. ValueError when there is none or two different ones, or when an array
    in it does not parse (what is inside is never read: a nested `[]` is no answer)."""
    obj = _loads(raw)
    if isinstance(obj, dict) and isinstance(obj.get("result"), str) and "findings" not in obj:
        if obj.get("is_error"):
            raise ValueError(f"the auditor reported an error: {obj['result'][:200]}")
        raw = obj["result"]
        obj = _loads(raw)
    if (got := _as_findings(obj)) is not None:
        return got
    dec, pos, lists = json.JSONDecoder(), 0, []
    while m := _OPEN.search(raw, pos):
        try:
            x, pos = dec.raw_decode(raw, m.start())
        except ValueError:
            if m[0] == "[":
                raise ValueError("a JSON array in the output does not parse") from None
            pos = m.start() + 1
            continue
        if (f := _as_findings(x)) and f not in lists:
            lists.append(f)
    if len(lists) != 1:
        raise ValueError(f"{len(lists)} different findings lists in the output" if lists else
                         "no findings list in the output (an empty one counts only as the whole answer)")
    return lists[0]


def _loads(s):
    try:
        return json.loads(s)
    except ValueError:
        return None


def report(raw, *, session, model, effort, trigger, target) -> dict:
    """The audit_report: findings without evidence dropped first (§9.3), then the schema. ValueError when invalid."""
    kept = []
    for f in findings(raw):
        ev = f.get("evidence")
        ev = [x for x in ev if isinstance(x, str) and x.strip()] if isinstance(ev, list) else []
        if ev:
            kept.append({**f, "evidence": ev})
    rep = {"auditor_session": session, "model": model, "effort": effort, "trigger": trigger, "target": target,
           "findings": kept}
    if errs := validate("audit_report", rep):
        raise ValueError(f"not an audit_report: {errs[0]}")
    return rep


def counts(rep) -> dict:
    out = {}
    for f in rep["findings"]:
        out[f["severity"]] = out.get(f["severity"], 0) + 1
    return dict(sorted(out.items()))


def report_path(root, session) -> Path:
    return state_dir(root) / "reports" / f"audit-{session}.json"


def fit(materials: dict, budget=MAX_BYTES) -> dict:
    """Materials in order of importance, each text cut to what is left of the budget."""
    out = {}
    for name, data in materials.items():
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)
        b = text.encode()
        if len(b) > budget:
            text = b[:max(0, budget - 60)].decode(errors="ignore") + "\n…（超出材料上限，已截断）\n"
        out[name] = text
        budget = max(0, budget - len(text.encode()))
    return out
