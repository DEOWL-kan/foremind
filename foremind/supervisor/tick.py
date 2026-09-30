"""One supervisor pass (DESIGN §1.1, §10.2): `tick(project, now)`; plus `pause`, `request_run` and `confirm_exit`.

The global lock `<FOREMIND_CONFIG_HOME>/supervisor.lock` is taken without waiting: held elsewhere -> one line, exit 0.
Project root `STOP` or `.foremind/paused` -> no automatic action at all (running sessions are left alone). Otherwise:
  1  read plans, batch headers, decisions/Q-*.json, quota telemetry (every enabled project's), and events with locks
     in one project-lock step (REQ-13) then L0 (supervisor/phases/audit.py l0), before any action: a new hard
     failure pauses, ends the pass
  2  minimal L0 (§20 I2, I52①): every started batch that gate.batch_merged finds merged is caught up to `merged`
     (event `reconciled` with `skipped`), each batch checked at most every supervisor.merged_check_min (times in
     .foremind/merged_checked.json; fetch and gh run under the global lock); skipping approved or delivered, or
     awaiting_audit when the tier audits before delivery, is a hard failure: event `l0_hard_failure` (cataloger_hold) + P0 notice
  3  finish actions that have an intent and no result by checking the fact per kind, never by redoing them: a seat
     or successor job -> the carrier and `seat_opened`, read again once the job is seen ended, for the job's own
     seat_open (a launch that never kicked off is closed, its claim given back only with exit evidence; the same
     for a seat_open / seat_continue of no job of ours once its process is gone, or past the seat job time limit
     without one); a delivery -> the inbox cursor and `program_delivery`; an
     inbox message -> the inbox file; a notification -> our own record (whether it went out is unknown: recorded,
     plus `notify_unsent` with unknown=true for the run report, never resent)
  4  quota per account x role group (quota.py; the state is the account's, at the user level, shared by every
     project; a project's old .foremind/quota.json is folded in once), then the ready queue (ready.py): planned -> ready
  5  sessions to be rid of, no model call: those left on finished batches, a stuck batch's stuck session, the claim
     of a seat that never kicked off, a successor that can no longer accept (closing retried with backoff; under
     manual, or once the backoff is at 30 minutes, the user answers with `foremind confirm-exit`), the idle seat of
     an auto batch left waiting for the gate, the audit or a merge (`seat_released`); with exit evidence their
     locks are broken. Told once: an approved plan edited by hand (`plan_unbound`), a batch delivered for the user
     to merge
  6  unless every unfinished batch waits on the user (full block: no new model call this tick, one summary notice
     per set of reasons, only API-error retries to holders already at work, each with the inbox pending before it,
     and the waiting notice once they are used up (m2c.2 r3, r4, REQ-3); a batch whose only blocking Q-n are
     `deciding` does not wait on the user): successors for
     started batches nobody works on (never while one opened before has not run
     `handoff --accept`: that one is watched for being stuck instead), stuck seats (stuck.py), gate runs after an
     approved receipt (retried while approved, or delivered at merge_dev or after a failed run of the gate the
     pre-delivery audit started, up to supervisor.seat_retries failures; not while a merge_after / depends_on batch
     is unmerged: `gate_waiting` once per such set), must-fix lists (changes_requested -> running), reviewers
     (review.start; with one-shot jobs at most supervisor.max_oneshot in flight), new seats (`foremind seat`),
     "continue" after quota exhaustion (one of this project's sessions per tick), inbox delivery to idle auto
     sessions (never to one
     handing off: `handoff --accept` forwards its inbox); then the phases (supervisor/phases: also under a full
     block, told so); then one quota probe per account (the reviewer's model, exclusions checked) if any of these
     wanted a model while quota was unknown
  (before 6, even under a full block) an open Q-n whose push never happened (its job died, or the config was
     unreadable) is pushed once it is older than the push job's time limit; one failing schema `pending` gets a
     tick_error instead (it still blocks its batches)
Every action writes an intent first and a result after; long ones (seat, successor, gate, probe) run as detached
jobs (job.start, with a time limit: event `sv_<kind>_timed_out`, a probe's `oneshot_interrupted`; the job id is in
the intent) harvested by a later tick, so the lock is never held while they run. One bad
file costs its own item a `tick_error`, not the pass. Nothing here asks a model to judge. Times are epoch seconds;
`now` can be injected.
"""
import contextlib
import importlib
import json
import os
import pkgutil
import subprocess
import sys
import time
import tomllib
import uuid
from datetime import datetime
from pathlib import Path

from foremind import (audit, carriers, config, gate, handoff, header, heartbeat, inbox, job, lock, review, schemas,
                      seat, worktree)
from foremind import notify as notifier
from foremind.events import EventLog
from foremind.fsutil import LockBusy, atomic_write, global_lock, project_lock, sha256_bytes
from foremind.paths import find_project_root, state_dir, user_config_dir
from foremind.plan import model
from foremind.state import BATCH, BATCH_SIDE, reconcile
from foremind.decide import pending
from foremind.defaults import TABLE
from foremind.supervisor import machine, phases, quota, ready, stuck
from foremind.vendors import claude

# jobs run in the project root: -P and the hooks' PYTHONPATH keep a `foremind/` there (foremind's own repo) from
# standing in for this package (§20 I53①)
PKG_PARENT = claude._PKG_PARENT  # the same path the hooks carry
FM = [sys.executable, "-P", "-m", "foremind"]
SUCCESSOR = [sys.executable, "-P", "-m", "foremind.commands.supervise"]  # + batch: out of `foremind`'s command list
NET_TIMEOUT_S = 120  # each git / gh call the pass makes itself through review.run (SF-4; set by the commands)
JOB_SLACK_S = 900  # on top of a job's own command timeouts: fetches, worktrees, gh
PID_SLACK_S = 2  # D23: ps lstart and started_at are both cut to the second
LIVE = ("running", "changes_requested")  # a seat is working on these
WAIT_TEXT = {"watch": "等你在场（或 foremind run）", "accompany": "等你在场（或 foremind run）",
             "user": "等你认领（foremind seat --user）"}
LOW_TEXT = "Foremind：额度偏低。到下一个安全点写交接段（`foremind handoff --write`），写完可以继续。"
RESUME_TEXT = "Foremind：额度已恢复，继续。"
GATE_TEXT = "Foremind：门禁未通过：\n{fails}\n处理后提交，再执行 `foremind review`。"
ERRORS = (OSError, ValueError, config.ConfigError, carriers.CarrierError, lock.LockError, review.FlowError,
          seat.SeatError, handoff.HandoffError, worktree.WorktreeError, inbox.InboxCorrupt, NotImplementedError)
KINDS = ("seat", "successor", "gate", "probe")  # jobs of the tick itself; phases add their KIND
IDLE_RELEASE = ("approved", "awaiting_audit", "delivered")  # finding 21: only the gate, the audit or a merge left
ANY = (Exception, SystemExit)  # what a phase may raise (r1: its sys.exit() too); KeyboardInterrupt still stops us
_PHASES = None  # the phase modules this process runs (Tick.load_phases)


class Reason(str):
    """A reason of Tick.waits: its text (what the block items, notices and status print, unchanged) plus `.code`,
    which status maps to the user's command without reading the text (m2d.9); `.qs`: the pendings of "decide"."""
    def __new__(cls, text, code, qs=()):
        r = super().__new__(cls, text)
        r.code, r.qs = code, list(qs)
        return r


def code_fingerprint() -> str:
    """Finding 23: the package's .py files by path, st_mtime_ns and size (r1: a file removed, or one put back with
    its old mtime by cp -p / rsync -t, changes it too); a `foremind supervise` holding another one runs other code."""
    pkg, out = PKG_PARENT / "foremind", []
    for p in sorted(pkg.rglob("*.py")):
        with contextlib.suppress(OSError):  # removed while we look
            st = p.stat()
            out.append(f"{p.relative_to(pkg)}\0{st.st_mtime_ns}\0{st.st_size}")
    return sha256_bytes("\n".join(out).encode())[:16]


def supervisor_path(root) -> Path:
    """{pid, started_at, code, argv} of the running `foremind supervise`, written at its start (and re-exec)."""
    return state_dir(root) / "supervisor.json"


def supervisor_state(root) -> tuple[str, dict]:
    """("stopped" | "stale" | "current", supervisor.json): stopped when there is none or its pid is gone, stale when
    its code is older than the package's now. D23: a live pid whose process started more than PID_SLACK_S after
    started_at is another process that got the pid since: stopped. Only later counts: a re-exec keeps the pid and its
    start time and writes a new started_at."""
    try:
        rec = json.loads(supervisor_path(root).read_text(encoding="utf-8"))
        alive = (pid := int(rec["pid"])) > 0 and job._alive(pid)
    except (OSError, ValueError, KeyError, TypeError):
        return "stopped", {}
    if not alive or ((at := _epoch(rec.get("started_at"))) and (st := _started(pid)) is not None
                     and st > at + PID_SLACK_S):
        return "stopped", rec
    return ("current" if rec.get("code") == code_fingerprint() else "stale"), rec


def _started(pid) -> float | None:
    """When process `pid` started (`ps -o lstart=`, to the second, local time); None when ps cannot tell."""
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5,
                           env={**os.environ, "LC_ALL": "C"})
        return time.mktime(time.strptime(r.stdout.strip(), "%a %b %d %H:%M:%S %Y"))
    except (OSError, subprocess.SubprocessError, ValueError, OverflowError):
        return None


def setting(cfg, key):
    v = cfg.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else TABLE[key]


def fm(*args) -> list[str]:
    return [*FM, *args]


def paused_path(root) -> Path:
    return state_dir(root) / "paused"


def pause(root, on: bool) -> bool:
    """`foremind pause` / `resume`; False when already so."""
    p = paused_path(root)
    if p.exists() == on:
        return False
    if on:
        atomic_write(p, datetime.now().astimezone().isoformat(timespec="seconds") + "\n")
    else:
        p.unlink()
    EventLog(state_dir(root) / "events.jsonl").append("paused" if on else "resumed")
    return True


def request_run(root, batches) -> None:
    """`foremind run`: record the request; the tick then opens these first and regardless of mode (presence), and
    even with quota low, until the batch's next `seat_opened`. Dependencies, decisions, exhausted quota, locks and
    disjointness still apply."""
    for b in batches:
        seat.read_header(root, b)  # unknown batch -> SeatError
    EventLog(state_dir(root) / "events.jsonl").append("run_requested", batches=list(batches))


def _exit_evidence(evs, session):
    """lock.ExitEvidence once `session`'s carrier confirmed a close, or the user ran `foremind confirm-exit` for it
    (§20 I52③); session names are never reused, so it stands for good. None otherwise."""
    for e in evs:
        if e.get("session") != session:
            continue
        if e["type"] == "exit_confirmed":
            return lock.ExitEvidence(session, e.get("carrier") or "manual", "user_confirmed")
        if e["type"] == "sv_close" and e.get("confirmed"):
            return lock.ExitEvidence(session, e["carrier"], e.get("how", "absent"))
    return None


def confirm_exit(root, bid) -> str:
    """`foremind confirm-exit <batch>` (§20 I52③): the user says the session the supervisor last failed to close for
    `bid` has exited. The next pass takes it as ExitEvidence(how="user_confirmed"). Prints that session before
    recording it (notices never name it) and returns it. Only the user runs this: the command refuses inside a
    Foremind session (M-1)."""
    seat.read_header(root, bid)  # unknown batch -> SeatError
    evs = list(handoff.history(root))
    tried = [e for e in evs if e["type"] == "sv_close" and e.get("batch") == bid
             and _exit_evidence(evs, e["session"]) is None]
    if not tried:
        raise seat.SeatError(f"{bid}: no session of this batch waits for an exit confirmation")
    s = tried[-1]["session"]
    print(f"{bid}: confirming that {s} has exited")
    EventLog(state_dir(root) / "events.jsonl").append("exit_confirmed", batch=bid, session=s,
                                                      carrier=tried[-1].get("carrier") or "manual")
    return s


def tick(project=None, now=None) -> int:
    root = Path(project).resolve() if project else find_project_root()
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(global_lock())
        except LockBusy:
            print("foremind tick: another supervisor pass holds the global lock; skipped")
            return 0
        return _tick(root, time.time() if now is None else now)


def _tick(root, now) -> int:
    # ponytail: the current project only; one supervisor over every enabled project (~/.config/foremind/projects,
    # quota per account across them) is M2-8
    if (root / "STOP").exists():
        print("foremind tick: STOP file in the project root: no automatic actions")
        return 0
    if paused_path(root).exists():
        print("foremind tick: paused (foremind resume to continue)")
        try:  # REQ-16 (m2b.6, controller r2 ruling): held P0s still come out; a notice, no automatic action
            from foremind.supervisor.phases import report as run_report
            if path := run_report.paused(root, user_cfg()):
                print(f"run report {path}")
        except ANY as e:
            print(f"foremind tick: paused run report: {type(e).__name__}: {e}", file=sys.stderr)
        return 0
    log, bad = EventLog(state_dir(root) / "events.jsonl"), None
    try:  # REQ-13: from reading the events through L0 whatever raises fails closed (r8), KeyboardInterrupt aside
        try:
            cfg = config.load(root)
        except config.ConfigError as e:  # §20 I17
            bad = ("config_invalid", "config", e)
        try:
            _events(root)
        except ValueError as e:  # not JSON, not UTF-8, not an event: no pass can read its facts
            bad = ("events_invalid", "events.jsonl", e)
        if bad:
            if bad[0] == "config_invalid":
                log.append("config_error", dedupe_id=f"config_error:{sha256_bytes(str(bad[2]).encode())[:16]}",
                           error=str(bad[2]))
            _invalid(root, *bad)
            print(f"foremind tick: {bad[2]}", file=sys.stderr)
            return 1
        t = Tick(root, cfg, now)
        stop = t.l0()
    except ANY as e:
        what = f"{type(e).__name__}: {e}"
        _invalid(root, "tick_crash", "tick", what)
        print(f"foremind tick: {what}", file=sys.stderr)
        return 1
    if not stop:
        t.run()
    for line in t.did:
        print(line)
    return 0


EVENT_KEYS = ("id", "ts", "type", "phase", "dedupe_id", "prev", "hash")  # on every event EventLog writes


def _events(root) -> list:
    """Every event, read under the events lock (a hook's line half appended is no invalid log). ValueError when a
    line is not JSON or not UTF-8, or is not an object with EVENT_KEYS: the tick, L0 and EventLog.append read them."""
    with EventLog(state_dir(root) / "events.jsonl")._lock():
        evs = list(handoff.history(root))
    for e in evs:
        if not isinstance(e, dict) or any(k not in e for k in EVENT_KEYS):
            raise ValueError(f"not an event: {str(e)[:80]}")
    return evs


def user_cfg():
    """The user layer alone: it picks the channel of an L0 P0 (r9: the project layers are #22 files under check,
    `foremind init` writes notify.channel there, a Bash can silence the P0 about itself). No notify.channel there but
    a [notify.ntfy] (user layer only, I52④): ntfy (r10: the channel init wrote is the project layer's)."""
    try:
        with open(user_config_dir() / "config.toml", "rb") as f:
            cfg = config.merge([config.Layer("user", tomllib.load(f))])
    except (OSError, ValueError, config.ConfigError):
        return config.merge([])
    if "notify.channel" not in cfg and isinstance(notifier.user_notify().get("ntfy"), dict):
        cfg["notify.channel"] = "ntfy"
    return cfg


def _invalid(root, check, target, e) -> None:
    """REQ-13: the config, events.jsonl or the audit phase does not load, or the pass raised before L0 let it act
    (tick_crash, fingerprinted with the exception's type and message), so neither L0 nor any action runs: an L0 hard
    failure, paused first (§9.2), one P0 per fact (since its last l0_cleared). An events.jsonl that cannot be read or
    written takes no event: the pause file alone, and the P0 sent straight, kept to one by that pause (a paused pass
    ends before this). Its channel is the user layer's (user_cfg)."""
    log, cfg = EventLog(state_dir(root) / "events.jsonl"), user_cfg()
    try:
        evs = _events(root)
    except (ValueError, OSError):
        evs = None
    try:
        gen = audit.generation(evs or [], check, target)
    except Exception:  # an l0_* event whose fields are no keys: what fails
        gen = None
    fp = audit.fingerprint(check, target, gen, e)
    did, title = f"l0_hard_failure:{fp}", "L0 对账：已暂停全部自动动作"
    body = ({"config_invalid": "配置读不了", "events_invalid": "事件日志读不了", "tick_crash": "监督器本轮出错"}
            .get(check, "L0 对账模块载入不了")
            + "，L0 对账与全部自动动作都做不了，已暂停。在本机执行 foremind audit 查看，修好后 foremind resume。")
    if evs and any(x["dedupe_id"] == did for x in evs):
        return  # reported, and resumed since: accepted (no pass acts until it loads)
    try:
        pause(root, True)
    except Exception:  # its `paused` event, events.jsonl not loading: the file is what pauses
        if not paused_path(root).exists():
            raise
    if evs is None:  # events.jsonl cannot be read: no event goes in, no dedupe record either
        notifier.get(root, cfg).send(title, body, "P0")
        return
    try:
        log.append("l0_hard_failure", dedupe_id=did, check=check, target=target, fingerprint=fp,
                   detail=str(e)[:300], severity="P0")
        notifier.notify(root, cfg, f"l0:{fp}", title, body, "P0")
    except (ValueError, OSError):  # events.jsonl cannot be read or written: no dedupe record either
        notifier.get(root, cfg).send(title, body, "P0")


def _epoch(ts) -> float:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return 0.0


class Tick:
    ERRORS = ERRORS

    def __init__(self, root, cfg, now):
        self.root, self.cfg, self.now = Path(root), cfg, now
        self.sd = state_dir(root)
        self.log = EventLog(self.sd / "events.jsonl")
        self.did, self.demand, self.evs, self._cfgs, self._machine = [], set(), [], {}, None
        self._index()
        self.phases = self.load_phases()
        self.after = {m.KIND: getattr(m, "after", None) for m in self.phases
                      if isinstance(getattr(m, "KIND", None), str)}
        # the account's quota state, every project's (quota.state_path): tick() holds the global lock it lives under
        self.qfile, self.qdirty = quota.load(), False
        with self.guard("quota", "migrate"):
            quota.migrate(self.root, self.qfile)
        self.checked_path = self.sd / "merged_checked.json"  # per project: {batch: last merged check}
        try:
            self.checked = dict(json.loads(self.checked_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            self.checked = {}

    def l0(self) -> bool:
        """REQ-13: the audit phase's L0, before any action; True when this pass must not act (a new hard failure,
        paused, or the checks could not run). Whatever it raises, _tick fails closed on (tick_crash)."""
        self.load()
        l0 = [m.l0 for m in self.phases if m.__name__ == f"{phases.__name__}.audit"]
        if not l0 and any(m.name == "audit" for m in pkgutil.iter_modules(phases.__path__)):
            # there but it did not import (phase_import): nothing checks, so nothing acts
            _invalid(self.root, "l0_error", "l0", "the audit phase did not load")
            self.did.append("no action: L0 could not check")
            return True
        return bool(l0 and l0[0](self))

    def run(self):
        """The actions of a pass that L0 let act."""
        self.reconcile()
        self.harvest_jobs()
        self.recover()
        self.load()
        self.quota()
        self.promote()
        self.harvest_reviews()
        self.load()
        self.close_predecessors()
        self.close_finished()
        self.release()
        self.release_idle()
        self.repush()
        self.announce()
        direct, why = self.waits()
        blocked = ready.full_block(self.headers, direct, why)
        full = blocked is not None
        if full:
            self.announce_block(blocked)
        else:
            self.seats_recover()
        for bid in self.order:  # a full block: only the API-error retries (resuming a turn is no new start, §10.2)
            if self.state(bid) == "running":
                with self.guard("stuck", bid):
                    stuck.check(self, bid, retries_only=full)
        if not full:
            self.gates()
            self.start_reviews()
            self.open_seats()
            self.resume_one()
        self.deliver(retries_only=full)
        self.run_phases(blocked is not None)
        if blocked is None:
            self.probes()  # last: every wish to call made this pass counts
        if self.qdirty:
            self.save_quota()

    def load_phases(self) -> list:
        """The modules of supervisor/phases not starting with `_`, by name (phases/__init__.py has the contract);
        found once per process (r2): a phase added since comes with the re-exec, not into the old code."""
        global _PHASES
        if _PHASES is None:
            _PHASES = self._find_phases()
        return _PHASES

    def _find_phases(self) -> list:
        out = []
        for m in sorted(pkgutil.iter_modules(phases.__path__), key=lambda m: m.name):
            if m.name.startswith("_"):
                continue
            try:
                out.append(importlib.import_module(f"{phases.__name__}.{m.name}"))
            except ANY as e:  # a broken phase costs itself, not the pass
                self.error("phase_import", m.name, e)
        return out

    def run_phases(self, blocked):
        for m in self.phases:
            try:
                m.run(self, blocked)
            except ANY as e:
                self.error("phase", m.__name__.rsplit(".", 1)[-1], e)

    def save_quota(self):
        quota.save(self.qfile)
        self.qdirty = False

    # --- state -----------------------------------------------------------------

    # ponytail: every pass loads plans, headers and the whole event log 3-4 times, and pending_successor, _opened,
    # failures, close_due scan all events per batch (O(batches x events)); index the events once per load if a
    # project's log grows past a few thousand events
    def load(self):
        self.plans, self.headers, self.plan_of, self._cfgs = {}, {}, {}, {}
        for pid in model.plan_ids(self.root):
            try:
                p = model.load(self.root, pid)
            except (model.PlanError, OSError) as e:
                self.error("plan", pid, e)
                continue
            self.plans[pid] = p
            for bid, d in p.batches.items():
                self.headers[bid], self.plan_of[bid] = d.header, pid
        self.order = list(self.headers)
        self.decisions, self.qfiles, self.qbad = [], [], set()
        for p in sorted((self.sd / "decisions").glob("Q-*.json")):
            try:
                q = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                self.error("decision", p.name, e)
                continue
            if not isinstance(q, dict):  # r2: told too; it names no batch to block
                self.error("decision", p.name, f"not a JSON object: {type(q).__name__}")
                continue
            if errs := schemas.validate("pending", q):  # r1: told once per first error, pushed or not; never pushed
                self.error("decision", p.name, errs[0])
                self.qbad.add(p.name)
                b = q.get("blocks") or []  # still blocks what it names (fail-closed), as ready.blocking compares it
                q = {**q, "id": str(q.get("id", "?")), "blocks": [str(x) for x in (b if isinstance(b, list) else [b])]}
            self.decisions.append(q)
            self.qfiles.append(p.name)
        # open_seat claims and writes its seat_open intent in one project-lock step; hooks append under the events
        # lock alone, taken after the project lock as audit._l0 does (r9: a line half appended is no tick_crash;
        # is_bound reads them too)
        with project_lock(self.root), self.log._lock():
            self.evs = list(handoff.history(self.root))
            self.holders = {b: lock.holder(self.root, b) for b in self.headers}
            self.bound = {pid: model.is_bound(self.root, p) for pid, p in self.plans.items()}
        self._index()

    def _index(self):
        self.ids = {e["id"] for e in self.evs}
        self.intents = {e["dedupe_id"]: e for e in self.evs if e["phase"] == "intent"}
        self.results = {e["dedupe_id"]: e for e in self.evs if e["phase"] == "result" and e["dedupe_id"]}

    def state(self, bid) -> str:
        return self.headers[bid].get("state", "planned")

    def emit(self, type, *, phase="result", dedupe_id=None, **fields) -> dict:
        e = self.log.append(type, phase=phase, dedupe_id=dedupe_id, at=self.now, **fields)
        if e["id"] not in self.ids:
            self.ids.add(e["id"])
            self.evs.append(e)
            if dedupe_id:
                (self.results if phase == "result" else self.intents)[dedupe_id] = e
        return e

    def result(self, did):
        return self.results.get(did)

    def open_intents(self, type, **match) -> list[dict]:
        return [e for d, e in self.intents.items() if e["type"] == type and d not in self.results
                and all(e.get(k) == v for k, v in match.items())]

    def error(self, where, what, e):
        msg = f"{where} {what}: {e}"
        self.emit("tick_error", dedupe_id=f"tick_error:{sha256_bytes(msg.encode())[:16]}", where=where,
                  what=str(what), error=str(e))
        if f"error: {msg}" not in self.did:  # r2: load runs 3-5 times a pass
            self.did.append(f"error: {msg}")

    @contextlib.contextmanager
    def guard(self, where, what):
        """SF-3: a bad file (inbox, header, receipt) costs its own item a tick_error, not the whole pass."""
        try:
            yield
        except self.ERRORS as e:
            self.error(where, what, e)

    def bcfg(self, bid):
        """The batch's merged config (task layer approved per plan.model.task_config_approved); None on a config
        error, which is recorded (§20 I17) and keeps the batch out of this tick's actions."""
        if bid not in self._cfgs:
            pid = self.plan_of[bid]
            try:
                ok = model.task_config_approved(self.root, self.plans[pid], bid, bound=self.bound[pid])
                self._cfgs[bid] = config.load(self.root, config.task_layer(self.headers[bid]), task_user_approved=ok)
            except config.ConfigError as e:
                self.emit("config_error", dedupe_id=f"config_error:{bid}:{sha256_bytes(str(e).encode())[:16]}",
                          batch=bid, error=str(e))
                self._cfgs[bid] = None
        return self._cfgs[bid]

    def mode(self, cfg) -> str:
        return cfg.get("delivery.depends_on", TABLE["delivery.depends_on"])

    def merge_dev(self, bid) -> bool:
        """Every repo of the batch delivers at merge_dev: the supervisor's gate run merges it (SF-7)."""
        cfg = self.bcfg(bid)
        return cfg is not None and all(review.repo_cfg(cfg, r, "level", "delivery.level", TABLE["delivery.level"])
                                       == "merge_dev" for r in self.headers[bid].get("repos", []))

    def requested(self) -> list[str]:
        """Batches asked for with `foremind run` and not opened since."""
        out = []
        for e in self.evs:
            if e["type"] == "run_requested":
                out += [b for b in e.get("batches", []) if b not in out]
            elif e["type"] == "seat_opened" and e.get("batch") in out:
                out.remove(e["batch"])
        return out

    def busy(self) -> set:
        """Batches whose paths are taken without a started state: a lock holder, or a seat job on its way."""
        return {b for b, s in self.holders.items() if s} | {b for b in self.headers if self.in_flight(b)}

    def in_flight(self, bid, kinds=("seat", "successor")) -> bool:
        return any(self.open_intents(f"sv_{k}", batch=bid) for k in kinds)

    def oneshots(self) -> int:
        """One-shot roles in flight, what supervisor.max_oneshot caps (m2b.3 r1): reviewers (`review_started`) and jobs
        started with a one-shot session (`oneshot_session`, oneshot.prepare's: the decider, the one-shot controller)."""
        return len(self.open_intents("review_started")) + sum(
            e["type"].startswith("sv_") and "oneshot_session" in e and d not in self.results
            for d, e in self.intents.items())

    def opening(self, bid) -> bool:
        """A seat_open or seat_continue of `bid` has begun and not ended (by a job of ours or the user's command)."""
        return bool(self.open_intents("seat_open", batch=bid) or self.open_intents("seat_continue", batch=bid))

    def failures(self, bid, kind, **match) -> int:
        """Failed `sv_<kind>` runs for the batch since it was last opened or asked for with `foremind run`. A gate
        error (exit 2) counts only while a merge of the batch is left without a result (merge_command, gh pr merge,
        merge_unverified, a group cut short: all exit 2 too, r1 #2). The merge intent is written once per head, so
        a retry failing at the same head adds no event (r2 #1): the open intents come from the whole log; a merge
        waiting in the host's queue is not a failed one. m2c.7 (m2b.10 r3): only an intent at the batch's current
        head counts (the heads of its latest event that has them: review_requested, gate_result, batch_state,
        batch_updated …); one at a head since replaced is left behind. Other gate errors (the batch busy, gh
        unreachable) do not count (M1-7-r3 note 7); timeouts and lost jobs do (SF-8). The gate after the
        pre-delivery audit (delivered, not merge_dev) has no merge: its errors are what it is retried for, and count
        (m2b.5 r11)."""
        n, open_, heads = 0, {}, {}
        for e in self.evs:
            if (e["type"] == "run_requested" and bid in e.get("batches", [])) or \
                    (e["type"] == "seat_opened" and e.get("batch") == bid):
                n = 0
            if e.get("batch") != bid:
                continue
            if isinstance(e.get("heads"), dict):
                heads.update(e["heads"])
            if e["type"] in ("merge", "merge_queued"):
                did = e.get("dedupe_id") or f"merge:{bid}:{e.get('repo')}:{e.get('head')}"
                if e["type"] == "merge" and e["phase"] == "intent":
                    open_[did] = (e.get("repo"), e.get("head"))
                else:
                    open_.pop(did, None)
            elif e["type"] == f"sv_{kind}" and e["phase"] == "result" and not e.get("ok") and \
                    all(e.get(k) == v for k, v in match.items()):
                merging = any(heads.get(r, h) == h for r, h in open_.values())  # a head not known yet: as before
                n += not (kind == "gate" and e.get("exit_code") == 2 and not merging
                          and (e.get("state") != "delivered" or self.merge_dev(bid)))
        return n

    def gave_up(self, bid) -> str | None:
        """Retries used up: seats and successors, or the gate runs of a delivered batch (merge_dev: its merge; else
        the success status after the pre-delivery audit); notified once, `foremind run <batch>` starts over."""
        cap = setting(self.cfg, "supervisor.seat_retries")
        kinds = (("seat", "开席", {}), ("successor", "继任", {}))
        if self.state(bid) == "delivered":
            kinds = (("gate", "合入" if self.merge_dev(bid) else "补写门禁", {"state": "delivered"}),)
        for kind, text, match in kinds:
            if (n := self.failures(bid, kind, **match)) >= cap:
                self.notify(f"{kind}_failed:{bid}:{n}", f"{bid} {text}失败",
                            f"连续 {n} 次没能{text}，已停止自动重试；处理后用 foremind run {bid} 再试。")
                return f"{text}失败 {n} 次"
        return None

    def unposted(self, bid) -> bool:
        """The last gate run of a delivered batch (the one the pre-delivery audit started, or its retry) failed: the
        success status is not written yet. A gate_result with verdict pass after that failure (m2b.5 r12: the user's
        own `foremind gate`, which writes no sv_gate) wrote it."""
        failed = False
        for e in self.evs:
            if e.get("batch") != bid:
                continue
            if e["type"] == "sv_gate" and e["phase"] == "result" and e.get("state") == "delivered":
                failed = not e.get("ok")
            elif e["type"] == "gate_result" and e.get("verdict") == "pass":
                failed = False
        return failed

    def job_timeout(self, bid, kind) -> int:
        """SF-8: a job that hangs (a fetch, a start or acceptance command) is killed and counts as failed."""
        cfg, h = self.bcfg(bid) or self.cfg, self.headers[bid]
        if kind == "gate":
            n = len(h.get("accept_commands", [])) + len(cfg.get("gate.checks") or TABLE["gate.checks"])
            timeout = int(cfg.get("acceptance.timeout_min", TABLE["acceptance.timeout_min"]))
            return timeout * 60 * max(1, n) + JOB_SLACK_S
        return (seat.setting(cfg, "seat.verify_timeout_s") * max(1, len(h.get("start_commands", [])))
                + seat.setting(cfg, "seat.manual_sessionstart_timeout_s") + JOB_SLACK_S)

    # --- quota -------------------------------------------------------------------

    def q(self, g) -> dict:
        return self.qfile.get("groups", {}).get(f"{quota.ACCOUNT}/{g}") or \
            {"state": quota.UNKNOWN, "since": self.now, "reason": "no_data", "fails": 0, "next_try": self.now}

    def qset(self, g, new):
        groups = self.qfile.setdefault("groups", {})
        old = groups.get(f"{quota.ACCOUNT}/{g}") or {}
        if new == old:
            return
        groups[f"{quota.ACCOUNT}/{g}"], self.qdirty = new, True
        if old.get("state") == new["state"] and old.get("window") == new.get("window"):
            return
        self.emit("quota_state", account=quota.ACCOUNT, group=g, frm=old.get("state"), to=new["state"],
                  **{k: new[k] for k in ("window", "resets_at", "reason") if k in new})
        self.did.append(f"quota {g}: {old.get('state')} -> {new['state']}")
        if g == "long" and old.get("state") == quota.EXHAUSTED:
            self.qfile["exhausted_ended"] = self.now  # stuck clocks restart here

    def may_call(self, g, *, new_seat=False) -> bool:
        """Low: no new seats (unless `foremind run` asked), half the one-shot concurrency; exhausted: nothing;
        unknown: nothing until a probe succeeds (the wish to call is what triggers the probe)."""
        st = self.q(g)["state"]
        if st == quota.UNKNOWN:
            self.demand.add(g)
        return st == quota.AVAILABLE or (st == quota.LOW and not new_seat)

    def quota(self):
        tel, cfg = quota.read_account_telemetry(self.root), quota.account_cfg(self.root, self.cfg)
        for g in quota.GROUPS:
            self.qset(g, quota.step(self.qfile.get("groups", {}).get(f"{quota.ACCOUNT}/{g}"), tel, self.now, cfg, g))
        st = self.q("long")
        if st["state"] == quota.EXHAUSTED:  # these get "continue" one by one on recovery: each project adds its own
            paused = self.qfile.get("paused") or []
            new = [s for b, s in self.holders.items() if s and s != lock.USER and self.state(b) in LIVE
                   and s not in paused]
            if new:
                self.qfile["paused"], self.qdirty = paused + new, True
        if st["state"] == quota.LOW:  # once per low spell
            for b in self.order:
                s = self.holders.get(b)
                if s and s != lock.USER and self.state(b) in LIVE:
                    self.say(s, LOW_TEXT, f"quota_low:{s}:{st['since']}")

    def resume_one(self):
        paused = self.qfile.get("paused") or []
        if not paused or not self.may_call("long"):  # unknown: the paused sessions are what a probe is for
            return
        rank = {}
        for i, b in enumerate(self.order):
            if (s := self.holders.get(b)) and s not in rank:
                rank[s] = i
        live = sorted((s for s in paused if s in rank), key=rank.get)
        if live:
            self.say(live[0], RESUME_TEXT, f"resume:{live[0]}:{self.qfile.get('exhausted_ended')}")
        # the list is every project's: this one drops only sessions it knows (its events), told now or gone
        known = {e.get("session") for e in self.evs} | set(rank)
        rest = [s for s in paused if s not in known] + live[1:]
        if rest != paused:
            self.qfile["paused"], self.qdirty = rest, True

    def probes(self):
        """One probe for the account, whichever role groups wanted to call while unknown (its answer holds for all,
        and for every project)."""
        due = [g for g in quota.GROUPS if g in self.demand and quota.probe_due(self.q(g), self.now)]
        if not due or self.open_intents("sv_probe"):
            return
        argv = self.cfg.get("quota.probe_command")
        if not argv:
            try:
                name = review.route(self.cfg)[0]  # the reviewer's model, exclude.models / providers checked
            except review.FlowError as e:
                self.error("probe", "model", e)
                return
            argv = ["claude", "-p", "--restricted", "--tools", "Read", "--permission-mode", "plan",
                    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--model", name, "--effort", "low",
                    "--", "Reply with the single word OK."]
        timeout = setting(self.cfg, "oneshot.timeout_min") * 60
        if self.start_job("probe", f"{'+'.join(due)}:t{int(self.now)}",
                          ["/bin/sh", "-c", argv] if isinstance(argv, str) else list(argv), timeout_s=timeout,
                          groups=due):
            for g in due:  # r1 #4: the state is every project's; none of them probes again while this one runs
                self.qset(g, {**self.q(g), "next_try": self.now + timeout})

    # --- actions -----------------------------------------------------------------

    def start_job(self, kind, key, argv, *, env=None, timeout_s=None, **fields) -> str | None:
        did = f"sv_{kind}:{key}"
        if did in self.intents:
            return None  # this very action was started before
        # the job id is in the intent: a crash before the sv_job link below loses nothing (harvest_jobs)
        jid = uuid.uuid4().hex
        self.emit(f"sv_{kind}", phase="intent", dedupe_id=did, job=jid, **fields)
        path = os.pathsep.join(filter(None, [str(PKG_PARENT), os.environ.get("PYTHONPATH")]))
        # a job is nobody's session: no identity inherited from a Foremind session that ran `foremind run`
        base = {"FOREMIND_SESSION": "", "FOREMIND_BATCH": "", "FOREMIND_ROLE": "",
                "FOREMIND_PROJECT": str(self.root), "PYTHONPATH": path}
        try:
            jid = job.start(self.sd / "jobs", argv, cwd=self.root, timeout_s=timeout_s, env={**base, **(env or {})},
                            job_id=jid)
        except OSError as e:
            self.emit(f"sv_{kind}", dedupe_id=did, ok=False, error=str(e), **fields)
            self.error(kind, key, e)
            return None
        self.emit("sv_job", dedupe_id=f"sv_job:{did}", action=did, job=jid)
        self.did.append(f"{fields.get('batch') or 'quota'}: {kind} job started")
        return jid

    def say(self, session, text, key) -> bool:
        """An inbox message, once per key. The sender carries a token of the key, so a crash between the append and
        its result is settled by looking at the inbox file."""
        did = f"sv_say:{key}"
        if did in self.results:
            return False
        token = sha256_bytes(did.encode())[:12]
        self.emit("sv_say", phase="intent", dedupe_id=did, session=session, token=token, text=text)
        try:
            inbox.append(session, text, sender=f"supervisor#{token}", root=self.root)
        except (OSError, ValueError, inbox.InboxCorrupt) as e:
            self.emit("sv_say", dedupe_id=did, session=session, ok=False, error=str(e))
            return False
        self.emit("sv_say", dedupe_id=did, session=session, ok=True)
        self.did.append(f"{session}: message {key.split(':')[0]}")
        return True

    def notify(self, key, title, body, priority="P1", cfg=None):
        """§20 I52②: title and body carry batch ids, options and a short line, never the project name. `cfg`: the
        config whose channel it takes, if not the tick's (an L0 P0: user_cfg())."""
        try:
            sent = notifier.notify(self.root, self.cfg if cfg is None else cfg, key, title, body, priority)
        except ValueError as e:
            self.error("notify", key, e)
            return
        if sent is not None:
            self.did.append(f"notify {key}: {'sent' if sent else 'NOT sent'}")

    def write_state(self, bid, to, *, expect, prior=None, event="batch_state", **fields) -> bool:
        """Program write of the header state (§20 I1) for moves seat.set_state cannot make: into a side branch
        (with state_prior) and the L0 catch-up to merged. The caller has checked the edge against the machine."""
        with project_lock(self.root):
            p = seat.header_path(self.root, bid)
            h, body = header.parse(p.read_text(encoding="utf-8"))
            frm = h.get("state", "planned")
            if frm not in expect:
                return False
            h["state"] = to
            if prior:
                h["state_prior"] = prior
            else:
                h.pop("state_prior", None)
                h.pop("blocked_reason", None)
            atomic_write(p, header.render(h, body))
            self.emit(event, batch=bid, frm=frm, to=to, **fields)
        self.headers[bid].clear()
        self.headers[bid].update(h)
        return True

    def carrier_for(self, bid, session):
        """The carrier the session was launched on (its seat_launch intent), with the batch's config (§5)."""
        kind = next((e.get("carrier") for e in reversed(self.evs) if e["type"] == "seat_launch"
                     and e["phase"] == "intent" and e.get("session") == session), None)
        cfg = self.bcfg(bid) or self.cfg
        return carriers.get(kind or seat.setting(cfg, "carrier.kind"), self.root, cfg)

    def close(self, bid, session, why=None):
        """Close through the carrier: lock.ExitEvidence when the carrier confirms the exit, else None. Every attempt
        is recorded (`sv_close`, `how` when confirmed, `why` when given), so the evidence stands for later passes
        (exited)."""
        car = ev = err = None
        try:
            car = self.carrier_for(bid, session)
            ev = car.close(session)
        except self.ERRORS as e:
            err = str(e)
            self.did.append(f"error: close {session}: {e}")
        self.emit("sv_close", batch=bid, session=session, carrier=car and car.name, confirmed=ev is not None,
                  **({"how": ev.how} if ev else {}), **({"error": err} if err else {}), **({"why": why} if why else {}))
        return ev

    def close_tries(self, session) -> list[dict]:
        return [e for e in self.evs if e["type"] == "sv_close" and e.get("session") == session]

    def close_due(self, session) -> bool:
        """SF-2: an unconfirmed close is tried again after 1, 2, 4 … (at most 30) minutes. Never under manual: its
        close only prints a request, and the user answers with `foremind confirm-exit`."""
        tries = self.close_tries(session)
        if not tries:
            return True
        if tries[-1].get("carrier") == "manual":
            return False
        return self.now - tries[-1].get("at", 0) >= min(60 * 2 ** (len(tries) - 1), 1800)

    def awaits_confirm(self, session) -> bool:
        """Only `foremind confirm-exit` is left: closed without evidence under manual, or the automatic retries are
        down to one per 30 minutes (6 tries, ~31 minutes)."""
        tries = self.close_tries(session)
        return bool(tries) and self.exited(session) is None and (tries[-1].get("carrier") == "manual"
                                                                 or len(tries) >= 6)

    def exited(self, session):
        return _exit_evidence(self.evs, session)

    def break_lock(self, bid, ev) -> bool:
        try:
            lock.break_lock(self.root, bid, ev)
        except lock.LockError as e:
            self.error("break_lock", bid, e)
            return False
        self.holders[bid] = None
        self.did.append(f"{bid}: lock of {ev.session} broken ({ev.how})")
        return True

    def successor(self, bid, why):
        """A successor seat through the job `python -m foremind.commands.supervise <batch>` (open_seat(successor=
        True)); never while one opened before has not accepted (MF-1), nor on a plan changed since approval (SF-9).
        REQ-5, REQ-6: one for a batch nobody holds takes a seat, so waits for a free one (`successor_deferred` once
        per attempt) and for the machine; one whose predecessor still holds the lock (a context handoff, also after
        its first successor got stuck, m2b.8 r3) takes over that seat, already counted, and waits for the machine
        only when that predecessor has exited (its lock not broken, m2e REQ-9). Returns what it waits for as the
        stuck notice says it (REQ-9), else None."""
        if (self.in_flight(bid) or self.pending_successor(bid) or not self.bound[self.plan_of[bid]]
                or self.failures(bid, "successor") >= setting(self.cfg, "supervisor.seat_retries")
                or not self.may_call("long")):
            return None
        n = 1 + sum(e["type"] == "sv_successor" and e.get("batch") == bid for e in self.intents.values())
        if not (s := self.holders.get(bid)):
            used, cap, _ = self.free_seats()
            if used >= cap:
                self.emit("successor_deferred", dedupe_id=f"successor_deferred:{bid}:{n}", batch=bid, used=used,
                          cap=cap)
                return f"继任在等名额（{used}/{cap}）"
        if (not s or self.exited(s) is not None) and not self.machine_ok():
            return "继任在等整机负载降下来"
        self.start_job("successor", f"{bid}:{n}", [*SUCCESSOR, bid], timeout_s=self.job_timeout(bid, "seat"),
                       batch=bid, why=why)
        return None

    def take_over(self, bid, session):
        """§10.4: close the stuck session and confirm it exited, break its lock (if it holds it) with that evidence,
        open a successor. Without evidence: notify; release() tries again, or the user runs confirm-exit."""
        ev = self.close(bid, session)
        if ev is not None and self.holders.get(bid) == session and not self.break_lock(bid, ev):
            ev = None
        wait = ev is not None and self.successor(bid, "stuck")
        self.notify(f"stuck:{bid}:{session}", f"{bid} 卡住",
                    "已标记 stuck。" + (f"旧会话已确认退出；{wait}。" if wait else
                                      "旧会话已确认退出，正在开继任会话。" if ev is not None else
                                      f"无法确认旧会话已退出：请关闭它，然后执行 foremind confirm-exit {bid}；"
                                      "确认前不会开继任。"))

    # --- facts about sessions ------------------------------------------------------------

    def _opened(self, bid=None, session=None):
        return [e for e in self.evs if e["type"] in ("seat_opened", "seat_user")
                and (bid is None or e.get("batch") == bid) and (session is None or e.get("session") == session)]

    def worktrees(self, bid) -> list[Path]:
        last = self._opened(bid=bid)
        return [Path(p) for p in (last[-1].get("worktrees") or {}).values()] if last else []

    def opened_at(self, session) -> float:
        return max((_epoch(e["ts"]) for e in self._opened(session=session)), default=0.0)

    def handed_off(self, bid, session, since=None) -> bool:
        """`session` wrote a handoff section for `bid` (at or after `since`, an ISO time, when given)."""
        t = _epoch(since) if since else 0.0
        return any(e["type"] == "handoff_written" and e.get("batch") == bid and e.get("session") == session
                   and _epoch(e["ts"]) >= t for e in self.evs)

    def succeeded(self, bid, session) -> bool:
        """A successor has been opened for `session`'s handoff (it takes the lock at `handoff --accept`)."""
        return any(e["type"] == "seat_opened" and e.get("successor") and e.get("batch") == bid
                   and e.get("predecessor") == session for e in self.evs)

    def _unaccepted(self, bid):
        """(session, predecessor) of the session last opened for `bid` if it was opened as a successor and has
        neither taken the lock (`handoff --accept`) nor been confirmed gone; else (None, None)."""
        last = next((e for e in reversed(self.evs) if e["type"] == "seat_opened" and e.get("batch") == bid), None)
        s = last.get("session") if last and last.get("successor") else None
        if not s or self.holders.get(bid) == s or self.exited(s) is not None:
            return None, None
        if any(e["type"] == "handoff_accept" and e["phase"] == "result" and e.get("batch") == bid
               and e.get("session") == s for e in self.evs):
            return None, None
        return s, last.get("predecessor")

    def pending_successor(self, bid) -> str | None:
        """MF-1: the unaccepted successor while it still can accept (the lock is its predecessor's or nobody's; S-3).
        Only one successor at a time; stuck.py watches this one."""
        s, pred = self._unaccepted(bid)
        return s if s and self.holders.get(bid) in (None, pred) else None

    def orphan_successor(self, bid) -> str | None:
        """S-3: an unaccepted successor that never can accept (someone else, e.g. the user, took the lock)."""
        s, pred = self._unaccepted(bid)
        return s if s and self.holders.get(bid) not in (None, pred) else None

    def stuck_session(self, bid) -> str | None:
        """The session `bid` was marked stuck for (its holder, or a successor that never accepted)."""
        return next((e.get("session") for e in reversed(self.evs) if e["type"] == "batch_state"
                     and e.get("batch") == bid and e.get("to") == "stuck"), None)

    def leftover(self, bid) -> str | None:
        """The session that has to be gone before `bid` moves on: a stuck batch's stuck session, or the claim a ready
        batch's seat left without a kickoff (§10.2 step 3). None while a seat is being opened."""
        st, s = self.state(bid), self.holders.get(bid)
        if self.in_flight(bid) or self.opening(bid):
            return None
        x = (self.stuck_session(bid) or s) if st == "stuck" else s if st == "ready" else None
        return None if x == lock.USER else x

    def needs_successor(self, bid) -> bool:
        """A started batch nobody works on: running / changes_requested without a holder, or stuck with its stuck
        session confirmed gone; and no seat on its way or waiting to accept."""
        st, s = self.state(bid), self.holders.get(bid)
        if s == lock.USER or self.in_flight(bid) or self.opening(bid) or self.pending_successor(bid):
            return False
        if st == "stuck":
            x = self.stuck_session(bid) or s
            return x is None or self.exited(x) is not None
        return st in LIVE and s is None

    # --- phases ------------------------------------------------------------------

    def reconcile(self):
        checked = self.checked
        every = setting(self.cfg, "supervisor.merged_check_min") * 60
        for bid in self.order:
            h = self.headers[bid]
            st = h.get("state", "planned")
            cur = h.get("state_prior") if st in BATCH_SIDE else st
            if cur not in seat.STARTED or self.now - checked.get(bid, 0) < every or (cfg := self.bcfg(bid)) is None:
                continue
            checked[bid] = self.now
            with self.guard("reconcile", bid):
                # now: a pass cut short later still keeps this check's limit
                atomic_write(self.checked_path, json.dumps(checked, indent=1, sort_keys=True))
                if not gate.batch_merged(self.root, bid, cfg):
                    continue
                _, skipped = reconcile(BATCH, cur, "merged")
                if not self.write_state(bid, "merged", expect=(st,), event="reconciled", skipped=list(skipped)):
                    continue
                self.did.append(f"{bid}: merged (reconciled from {cur})")
                if {"approved", "delivered"} & set(skipped) or \
                        ("awaiting_audit" in skipped and gate.pre_delivery_audit(h, cfg)):
                    # ponytail: M1 has no auditor to trigger; the event holds the cataloger back once there is one
                    e = self.emit("l0_hard_failure", batch=bid, check="state_vs_fact", skipped=list(skipped),
                                  severity="P0", cataloger_hold=True)
                    self.notify(e["id"], f"{bid} 未经门禁已合入",
                                f"{bid} 已在目标分支，记录停在 {cur}，已补记 merged。请复核；审计结束前不跑编目员。", "P0")

    def harvest_jobs(self):
        ended = []
        for kind in (*KINDS, *(k for k in self.after if k not in KINDS)):
            for it in self.open_intents(f"sv_{kind}"):
                link = self.result(f"sv_job:{it['dedupe_id']}")
                jid = link["job"] if link else it.get("job")  # no link: start_job was cut short (its id is here)
                try:
                    st = job.status(self.sd / "jobs", jid) if jid else {"state": "lost"}
                except OSError:  # no such job: cut short before job.start made it
                    st = {"state": "lost"}
                if st["state"] not in ("starting", "running"):
                    ended.append((kind, it["dedupe_id"], jid, st))
        if ended:
            self.load()  # MF-2: what a job wrote before it ended (seat_opened, its seat_open result) is read now
        for kind, did, jid, st in ended:
            it = self.intents[did]
            if "batch" in it and it["batch"] not in self.headers:
                continue  # its plan is unreadable this pass: harvested once it reads again (S-2)
            with self.guard("harvest", did):
                keep = {k: it[k] for k in ("batch", "groups", "state") if k in it}
                ok = st["state"] == "done" and st.get("exit_code") == 0
                # settle first: the result closes the intent, and nothing looks at this job again (S-2)
                try:
                    if kind in KINDS:
                        getattr(self, f"_after_{kind}")(it, ok, st, jid)
                    elif self.after[kind]:
                        self.after[kind](self, it, ok, st, jid)
                except ANY as e:  # not only ERRORS (a bug, a malformed file): the job waits, the pass goes on
                    self.error("harvest", did, e)
                    continue
                if st.get("timed_out"):
                    name = "oneshot_interrupted" if kind == "probe" else f"sv_{kind}_timed_out"
                    self.emit(name, dedupe_id=f"{name}:{did}", action=did, job=jid, **keep)
                self.emit(it["type"], dedupe_id=did, ok=ok, exit_code=st.get("exit_code"), job_state=st["state"],
                          **keep)

    def settle_open(self, e):
        """A seat_open / seat_continue intent without a result (§10.2 step 3): ok when its `seat_opened` (written
        after the kickoff went out) is there; else whatever may have started is closed, and a claim goes back only
        with exit evidence (release() retries the close)."""
        bid, s = e["batch"], e["session"]
        opened = bool(self._opened(bid=bid, session=s))
        if not opened:
            ev = self.close(bid, s)
            if ev is not None and lock.holder(self.root, bid) == s:
                self.break_lock(bid, ev)
        self.emit(e["type"], dedupe_id=e["dedupe_id"], batch=bid, session=s, ok=opened, recovered=True)
        self.did.append(f"{bid}: {e['type']} of {s} settled ({'opened' if opened else 'not opened'})")

    def _after_seat(self, it, ok, st, jid):
        """Settle what the seat job left open: its own seat_open (`job`, from FOREMIND_JOB), not one of the user's
        `foremind seat` (recover's); one written before m2b.10 (no `job`) as any of the batch's after the intent."""
        after = self.evs[self.evs.index(it):]
        for e in [e for e in after if e["type"] == "seat_open" and e["phase"] == "intent"
                  and e.get("batch") == it["batch"] and ("job" not in e or (jid and e["job"] == jid))
                  and e["dedupe_id"] not in self.results]:
            self.settle_open(e)
        if not ok:
            self.did.append(f"{it['batch']}: {it['type'][3:]} job failed ({st.get('exit_code', st['state'])})")

    _after_successor = _after_seat

    def _after_gate(self, it, ok, st, jid):
        bid = it["batch"]
        if ok or st.get("exit_code") != 1 or not jid:
            return
        try:
            if seat.read_header(self.root, bid).get("state") != "in_review":
                return  # approved and waiting (CI): the retry says nothing new
            out = (self.sd / "jobs" / jid / "stdout.log").read_text(encoding="utf-8", errors="replace")
        except (OSError, seat.SeatError):
            return
        fails = [x for x in out.splitlines() if x.startswith("FAIL")]
        s = lock.holder(self.root, bid)
        if fails and s and s != lock.USER:
            self.say(s, GATE_TEXT.format(fails="\n".join(fails)), f"gate_failed:{jid}")

    def _after_probe(self, it, ok, st, jid):
        tel = quota.read_account_telemetry(self.root) if ok else None
        for g in it.get("groups", []):
            self.qset(g, quota.probe_done(self.q(g), ok, self.now, self.cfg, g, tel))
        self.did.append(f"quota: probe {'ok' if ok else 'failed'}")

    def recover(self):
        """Intents left open by a crash in an earlier tick, settled by the facts."""
        due = []
        for it in self.open_intents("seat_open") + self.open_intents("seat_continue"):
            bid, pid = it.get("batch"), it.get("pid")
            # its process is gone (m2b.10: seat_open carries it): nothing will write the result
            gone = isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and not job._alive(pid)
            if bid not in self.headers or (self.in_flight(bid) and not gone):  # a job of ours: its harvest settles it
                continue
            with self.guard("recover", it["dedupe_id"]):
                # S-1: without a pid, e.g. its terminal was closed
                if gone or self.now - _epoch(it["ts"]) > self.job_timeout(bid, "seat"):
                    due.append(it["dedupe_id"])
        if due:  # r1 #1 (MF-2): seen gone first, so what it wrote before it exited (seat_opened, its result) is read now
            self.load()
        for did in due:
            it = self.intents[did]
            if did in self.results or it["batch"] not in self.headers:
                continue
            with self.guard("recover", did):
                self.settle_open(it)
        for it in self.open_intents("sv_say"):
            with self.guard("recover", it["dedupe_id"]):
                s, did = it["session"], it["dedupe_id"]
                try:
                    there = f" supervisor#{it['token']} ".encode() in (self.sd / "inbox" / f"{s}.md").read_bytes()
                except OSError:
                    there = False
                if not there:
                    inbox.append(s, it["text"], sender=f"supervisor#{it['token']}", root=self.root)
                self.emit("sv_say", dedupe_id=did, session=s, ok=True, recovered=True, appended=not there)
        for it in self.open_intents("sv_deliver"):
            with self.guard("recover", it["dedupe_id"]):
                s, upto = it["session"], it["upto"]
                after = self.evs[self.evs.index(it):]
                sent = next((e for e in after if e["type"] == "program_delivery" and e["phase"] == "intent"
                             and e.get("session") == s), None)
                if sent is not None and (r := self.result(sent["dedupe_id"])) is not None and not r.get("ok"):
                    sent = None  # the carrier reported the send failed
                with inbox.locked(s, root=self.root):
                    pending = inbox.pending_messages(s, root=self.root)
                    done = not any(m.end <= upto for m in pending)  # the cursor is already past upto
                    if not done and sent is not None:
                        inbox.mark_delivered(s, upto, root=self.root)
                self.emit("sv_deliver", dedupe_id=it["dedupe_id"], session=s, ok=done or sent is not None,
                          recovered=True)
        for it in self.open_intents("notify"):  # sent or not is unknown: recorded, never resent (at most once)
            with self.guard("recover", it["dedupe_id"]):
                self.emit("notify", dedupe_id=it["dedupe_id"], key=it.get("key"), sent=None, recovered=True)
                self.emit("notify_unsent", key=it.get("key"), channel=it.get("channel"), priority=it.get("priority"),
                          title=it.get("title"), unknown=True)

    def promote(self):
        busy = self.busy()
        for bid in self.order:
            if self.state(bid) != "planned" or not self.bound[self.plan_of[bid]] or (cfg := self.bcfg(bid)) is None:
                continue
            if ready.why_not_ready(bid, self.headers, self.decisions, self.mode(cfg), busy):
                continue
            try:
                seat.set_state(self.root, bid, "ready")
            except self.ERRORS as e:
                self.error("ready", bid, e)
                continue
            self.headers[bid]["state"] = "ready"
            self.did.append(f"{bid}: ready")

    def harvest_reviews(self):
        for it in self.open_intents("review_started"):
            bid, s = it.get("batch"), it.get("reviewer_session")
            if bid not in self.headers:
                continue
            try:
                meta = json.loads((self.sd / "reviews" / s / "meta.json").read_text(encoding="utf-8"))
                if job.status(self.sd / "jobs", meta["job_id"]).get("timed_out"):  # §1.7: killed at the timeout
                    self.emit("oneshot_interrupted", dedupe_id=f"oneshot_interrupted:{s}", batch=bid,
                              role="reviewer", session=s)
            except (OSError, ValueError, KeyError, TypeError):
                pass  # harvest() records what is missing
            try:
                r = review.harvest(self.root, bid, s, self.bcfg(bid) or self.cfg)
            except self.ERRORS as e:
                self.did.append(f"{bid}: review failed: {e}")
                continue
            if r is not None:
                self.did.append(f"{bid}: review r{r['round']} {r['verdict']}")

    def close_predecessors(self):
        """After `handoff --accept` moved the lock, close the session it came from (if it holds nothing else),
        retried per close_due until there is exit evidence."""
        seen = set()
        for e in list(self.evs):
            p, bid = e.get("predecessor"), e.get("batch")
            if (e["type"] != "seat_opened" or not e.get("successor") or not p or p == lock.USER or p in seen
                    or bid not in self.headers):
                continue
            seen.add(p)
            with self.guard("close", p):
                if self.exited(p) is None and self.close_due(p) and not lock.held_by(self.root, p):
                    self.close(bid, p)

    def close_finished(self):
        """Sessions left on merged, cataloged or cancelled batches, holding nothing unfinished, are closed through
        their carrier, retried per close_due (S-4: its lock may keep later batches out); with evidence their lock is
        given back. Once only `foremind confirm-exit` is left (awaits_confirm), the user is told once."""
        for bid in self.order:
            if self.state(bid) not in ready.FINISHED:
                continue
            for s in {self.holders.get(bid), self.pending_successor(bid)} - {None, lock.USER}:
                if any(h == s and self.state(b) not in ready.FINISHED for b, h in self.holders.items()):
                    continue  # it went on to another batch
                with self.guard("close", s):
                    ev = self.exited(s) or (self.close(bid, s) if self.close_due(s) else None)
                    if ev is not None and self.holders.get(bid) == s:
                        self.break_lock(bid, ev)
                    elif ev is None and self.holders.get(bid) == s and self.awaits_confirm(s):
                        self.notify(f"exit_unconfirmed:{bid}:{s}", f"{bid} 旧会话未确认退出",
                                    f"{bid} 已结束，但无法确认它的会话已退出，锁未释放，可能挡住后续批次。"
                                    f"请关闭它，然后执行 foremind confirm-exit {bid}。")

    def release(self):
        """Stuck sessions and claims left without a kickoff (leftover), and successors that can no longer accept
        (orphan_successor): closed, retried per close_due; with exit evidence (carrier, or `foremind confirm-exit`)
        a lock they hold is broken. No model call, so also in a full block."""
        for bid in self.order:
            if not (x := self.leftover(bid) or self.orphan_successor(bid)):
                continue
            with self.guard("release", bid):
                ev = self.exited(x) or (self.close(bid, x) if self.close_due(x) else None)
                if ev is not None and self.holders.get(bid) == x:
                    self.break_lock(bid, ev)
                elif (ev is None and self.holders.get(bid) == x and self.state(bid) == "ready"
                      and self.awaits_confirm(x)):  # S-A: an interrupted seat's claim (stuck ones: take_over tells)
                    self.notify(f"exit_unconfirmed:{bid}:{x}", f"{bid} 开席中断",
                                f"{bid} 的开席没有完成，无法确认会话已退出，认领未释放，可能挡住后续批次。"
                                f"请关闭它，然后执行 foremind confirm-exit {bid}。")

    def release_idle(self):
        """Finding 21: the seat of an auto batch that only waits for the gate, the audit or a merge (IDLE_RELEASE) is
        given up once idle (no tool open, not handing off, inbox empty; idle as for a delivery, a Stop only when later
        than the last delivery; not under manual):
        closed as in close_finished, retried per close_due without a notice; with exit evidence its lock is broken
        (`seat_released`). A later changes_requested gets a successor (needs_successor), its list in L1's status.
        m2b.1 r3: a session with exit evidence, or one its carrier finds gone, cannot be working: none of the idle
        checks, its lock is broken (after a close for the evidence); one the carrier cannot tell about (alive None) is
        never taken for gone. m2b.8 r1: a tool left open on an idle screen is cleared as stuck.check does."""
        for bid in self.order:
            s = self.holders.get(bid)
            if self.state(bid) not in IDLE_RELEASE or self.headers[bid].get("mode") != "auto" or s in (None, lock.USER):
                continue
            with self.guard("release_idle", bid):
                # once we close it: until evidence
                if self.exited(s) is None and not any(e.get("why") == "idle" for e in self.close_tries(s)):
                    car = self.carrier_for(bid, s)
                    alive = None if car.name == "manual" else car.read_state(s).alive
                    if alive is None or (alive and not self._idle_seat(bid, s, car)):
                        continue
                ev = self.exited(s) or (self.close(bid, s, "idle") if self.close_due(s) else None)
                if ev is not None and self.break_lock(bid, ev):
                    self.emit("seat_released", batch=bid, session=s, state=self.state(bid))

    def _idle_seat(self, bid, s, car) -> bool:
        """release_idle's checks for a live session: no tool open, not handing off, inbox empty, idle."""
        hb = stuck.clear_tool(self, bid, s, heartbeat.read(self.root, s) or {})
        if hb.get("tool_open") or hb.get("handoff_requested") or inbox.pending_messages(s, root=self.root):
            return False
        return self.idle(car, s, hb, since=self.delivered_at(s))

    def announce(self):
        """Stops the user may not see otherwise, each told once: an approved plan edited by hand since (findings 19:
        no seat nor successor opens on it; per plan_hash), a batch delivered for the user to merge (20: per the
        batch_state event that moved it there)."""
        for pid, p in self.plans.items():
            if self.bound[pid] or "approved_at" not in p.doc.header or not p.active():
                continue  # never approved: waits() says 计划待批准
            h = model.plan_hash(p)
            self.emit("plan_unbound", dedupe_id=f"plan_unbound:{pid}:{h}", plan=pid, plan_hash=h)
            self.notify(f"plan_unbound:{pid}:{h}", f"计划 {pid} 未绑定",
                        f"计划 {pid} 在批准后被手改（批次头、正文或 plan.md），调度已停：不再提升批次、开席、开继任；"
                        f"改回原样，或用 foremind plan amend {pid} 重新走修订。")
        for bid in self.order:
            if self.state(bid) != "delivered" or self.bcfg(bid) is None or self.merge_dev(bid):
                continue
            e = next((e for e in reversed(self.evs) if e["type"] == "batch_state" and e.get("batch") == bid
                      and "delivered" in (e.get("state"), e.get("to"))), None)
            self.notify(f"delivered:{bid}:{e['id'] if e else '-'}", f"{bid} 已交付，等你合入",
                        f"{bid} 已交付，等你合入；合入后监督进程补记 merged。")

    def waits(self):
        """({batch: reason} of batches waiting on the user, {batch: why_not_ready} of the other planned/ready)."""
        direct, why = {}, {}
        busy, asked = self.busy(), self.requested()
        deciding = {q["id"] for q in self.decisions  # a phase answers those
                    if q.get("state") == "deciding" and isinstance(q.get("id"), str)}
        for bid in self.order:
            h, st = self.headers[bid], self.state(bid)
            if st in ready.FINISHED:
                continue
            allq = ready.blocking(bid, self.decisions)
            qs = [q for q in allq if q not in deciding]  # still not ready (promote)
            holder, cfg = self.holders.get(bid), self.bcfg(bid)
            x = self.leftover(bid)
            reason = (Reason(f"待决 {'、'.join(qs)}", "decide", qs) if qs
                      else Reason("待决", "pending") if not allq and st == "blocked"
                      and h.get("blocked_reason") == "pending"
                      else Reason("失败，等你决定", "failed") if st == "failed"
                      else Reason("等你合入", "land") if st == "delivered" and not self.merge_dev(bid)
                      else (Reason("等审计放行", "audit_held") if audit.held(self.evs, bid) else None)
                      if st == "awaiting_audit"  # held: P0/P1, or no report; until then the auditor is due,
                      # whoever holds the batch
                      else Reason("你在做", "user") if holder == lock.USER
                      else Reason(f"{'卡住' if st == 'stuck' else '开席中断'}，需确认旧会话已退出（foremind confirm-exit "
                                  f"{bid}）", "confirm_exit") if x and self.awaits_confirm(x)
                      else Reason("配置有误", "config") if cfg is None
                      else (Reason("计划批准后被改动，调度已停", "plan_unbound")
                            if "approved_at" in self.plans[self.plan_of[bid]].doc.header
                            else Reason("计划待批准", "approve_plan")) if not self.bound[self.plan_of[bid]]
                      and (st in ("planned", "ready") or self.needs_successor(bid))
                      else None)
            if not reason and st in ("planned", "ready") and bid not in asked and h.get("mode") in WAIT_TEXT:
                reason = Reason(WAIT_TEXT[h["mode"]], "claim" if h["mode"] == "user" else "present")
            if not reason and st in ("ready", "running", "changes_requested", "stuck", "delivered") and \
                    (g := self.gave_up(bid)):
                reason = Reason(g, "gave_up")
            if reason:
                direct[bid] = reason
            elif st in ("planned", "ready"):
                why[bid] = ready.why_not_ready(bid, self.headers, self.decisions, self.mode(cfg), busy)
        return direct, why

    def announce_block(self, blocked):
        """Kept as `self.block` (fingerprint, items) for the report phase, whose run report summary is the one push
        per fingerprint (m2b.6)."""
        items = sorted(f"{b}：{r}" for b, r in blocked.items())
        self.block = (sha256_bytes(json.dumps(items, ensure_ascii=False).encode())[:16], items)
        self.did.append(f"full block: every unfinished batch waits on you ({len(items)}); no new model call this tick")

    def seats_recover(self):
        """Started batches nobody works on get a successor (its handoff record, or record "none", said so in the
        kickoff); a claim or stuck session in the way is release()'s."""
        for bid in self.order:
            if self.needs_successor(bid) and not self.gave_up(bid):
                self.successor(bid, "stuck" if self.state(bid) == "stuck" else "no lock holder")

    def gates(self):
        """After an approved receipt the gate runs once (as the supervisor, so a merge_dev batch can merge); an
        approved batch waiting on CI, or a delivered one whose merge did not happen (merge_dev, SF-7) or whose gate
        after the pre-delivery audit failed (unposted), is retried every supervisor.gate_retry_min, the latter until
        gave_up. changes_requested -> the must-fix list goes to the
        holder's inbox once per round, and the batch is running again (SF-1: stuck.py watches the session while it
        fixes)."""
        reviewing = {e.get("batch") for e in self.open_intents("review_started")}
        for bid in self.order:
            st = self.state(bid)
            retry = st == "approved" or (st == "delivered" and (self.merge_dev(bid) or self.unposted(bid))
                                         and not self.gave_up(bid))
            if (st not in ("in_review", "changes_requested") and not retry) or bid in reviewing:
                continue
            if retry and (ups := self.unmerged_upstream(bid)):  # finding 17: its merge_after check would only fail
                self.emit("gate_waiting", dedupe_id=f"gate_waiting:{bid}:{','.join(ups)}", batch=bid, upstream=ups)
                continue
            with self.guard("gates", bid):
                self._gate(bid, st, retry)

    def unmerged_upstream(self, bid) -> list[str]:
        """merge_after and depends_on batches not merged yet, sorted (gate's merge_after check); one this pass does
        not know counts as not merged."""
        h = self.headers[bid]
        ups = {m["batch"] for m in h.get("merge_after", [])} | set(h.get("depends_on", []))
        return sorted(b for b in ups if self.headers.get(b, {}).get("state", "planned") not in ready.DONE)

    def _gate(self, bid, st, retry):
        rs = review.receipts(self.root, bid)
        if not rs:
            return
        n, path = rs[-1]
        r = json.loads(path.read_text(encoding="utf-8"))
        if st == "changes_requested":
            s = self.holders.get(bid)
            if s and s != lock.USER and r.get("verdict") == "changes_requested":
                key = f"changes:{bid}:r{n}"
                self.say(s, seat.changes_text(n, r.get("issues", [])), key)
                if (res := self.result(f"sv_say:{key}")) and res.get("ok"):
                    seat.set_state(self.root, bid, "running")
                    self.headers[bid]["state"] = "running"
                    self.did.append(f"{bid}: running (must-fix list of r{n} sent)")
            return
        if self.in_flight(bid, ("gate",)) or (st == "in_review" and r.get("verdict") != "approved"):
            return
        # in_review: until the gate has answered this receipt (exit 0 or 1); an error (exit 2) is retried
        runs = [e for e in self.evs if e["type"] == "sv_gate" and e.get("batch") == bid
                and (retry or e.get("round") == n)]
        if st == "in_review" and any(self.result(e["dedupe_id"]).get("exit_code") in (0, 1)
                                     for e in runs if e["phase"] == "intent" and self.result(e["dedupe_id"])):
            return
        last = max((e["at"] for e in runs if e["phase"] == "intent"), default=None)
        if last is None or self.now - last >= setting(self.cfg, "supervisor.gate_retry_min") * 60:
            self.start_job("gate", f"{bid}:r{n}:t{int(self.now)}", fm("gate", bid), env={"FOREMIND_ROLE": "supervisor"},
                           timeout_s=self.job_timeout(bid, "gate"), batch=bid, round=n, state=st)

    def start_reviews(self):
        cap = setting(self.cfg, "supervisor.max_oneshot")
        if self.q("oneshot")["state"] == quota.LOW:
            cap = max(1, cap // 2)
        n = self.oneshots()
        for bid in self.order:
            if self.state(bid) != "review_ready" or n >= cap or (cfg := self.bcfg(bid)) is None:
                continue
            if not self.may_call("oneshot"):
                return
            try:
                s = review.start(self.root, bid, cfg)
            except self.ERRORS as e:  # e.g. commits after `foremind review`: the seat has to ask again
                key = f"review_start_failed:{bid}:{sha256_bytes(str(e).encode())[:16]}"
                self.emit("review_start_failed", dedupe_id=key, batch=bid, error=str(e))
                if (s := self.holders.get(bid)) and s != lock.USER:
                    self.say(s, f"Foremind：审查者没有启动：{e}", key)
                self.did.append(f"{bid}: reviewer not started: {e}")
                continue
            n += 1
            self.did.append(f"{bid}: reviewer {s} started")

    def repush(self):
        """m2a.2 r3: a reused Q-n starts no push job, so one whose first push never happened would wait unseen."""
        created = {e.get("question"): e for e in self.evs if e["type"] == "pending_created"}
        handled = {e["dedupe_id"] for e in self.evs if e["type"] == "notify"}
        for name, q in zip(self.qfiles, self.decisions):
            c = created.get(qid) if isinstance(qid := q.get("id"), str) else None
            if (q.get("state", "open") not in ready.OPEN_Q or f"notify:pending:{qid}" in handled or c is None
                    or self.now - _epoch(c["ts"]) < pending.NOTIFY_TIMEOUT_S or name in self.qbad):
                continue  # only open, never pushed, its own push job has had its time; well-formed (m2a.7 r1: load)
            with self.guard("repush", qid):
                if pending.push(self.root, self.cfg, q, c.get("request")) is not None:
                    self.did.append(f"{qid}: pushed")

    def free_seats(self) -> tuple[int, int, int]:
        """REQ-5, the one place seats are counted: (used, cap, waiting). used: batches held by a session other than
        the user's and not finished, plus seat and successor jobs on their way and successors yet to accept; cap:
        supervisor.max_seats, halved while quota is low; waiting: batches nobody holds that seats_recover would
        give a successor (they go before new batches; one whose predecessor holds the lock is in used already)."""
        cap = setting(self.cfg, "supervisor.max_seats")
        if self.q("long")["state"] == quota.LOW:
            cap = max(1, cap // 2)
        used = {b for b, s in self.holders.items() if s and s != lock.USER and self.state(b) not in ready.FINISHED}
        used |= {b for b in self.headers if self.in_flight(b) or self.pending_successor(b)}
        waiting = sum(not self.holders.get(b) and self.needs_successor(b) and self.bound[self.plan_of[b]]
                      and not self.gave_up(b) for b in self.order)
        return len(used), cap, waiting

    def machine_ok(self) -> bool:
        """REQ-6: False (`seat_deferred`) while the 1-minute load is at supervisor.max_load_per_cpu x CPUs or more,
        or available memory is under supervisor.min_free_mem_mb. Read once a pass; a figure that cannot be read
        does not limit (`machine_unreadable`). Both events once until the next seat opens."""
        m, first = self._machine, self._machine is None
        if first:
            m = self._machine = machine.read()
        opened = next((e["id"] for e in reversed(self.evs) if e["type"] == "seat_opened"), "none")
        if first:
            for what, err in m["errors"].items():
                self.emit("machine_unreadable", dedupe_id=f"machine_unreadable:{what}:{opened}", what=what, error=err)
        per_cpu = setting(self.cfg, "supervisor.max_load_per_cpu")
        min_mb = setting(self.cfg, "supervisor.min_free_mem_mb")
        reason = ("load" if m["load1"] is not None and m["cpus"] and m["load1"] >= per_cpu * m["cpus"] else
                  "memory" if m["avail_mb"] is not None and m["avail_mb"] < min_mb else None)
        if reason:
            self.emit("seat_deferred", dedupe_id=f"seat_deferred:{opened}", reason=reason, load1=m["load1"],
                      cpus=m["cpus"], avail_mb=m["avail_mb"] and round(m["avail_mb"]), max_load_per_cpu=per_cpu,
                      min_free_mem_mb=min_mb)
            if (msg := f"machine busy ({reason}): no seat opened this pass") not in self.did:
                self.did.append(msg)
        return reason is None

    def open_seats(self):
        used, cap, waiting = self.free_seats()
        free = cap - used - waiting
        asked, busy = self.requested(), self.busy()
        for bid in [b for b in asked if b in self.headers] + [b for b in self.order if b not in asked]:
            if self.state(bid) != "ready" or bid in busy or not self.bound[self.plan_of[bid]]:
                continue
            if (cfg := self.bcfg(bid)) is None or (self.headers[bid].get("mode") != "auto" and bid not in asked):
                continue
            if self.gave_up(bid) or ready.why_not_ready(bid, self.headers, self.decisions, self.mode(cfg), busy):
                continue
            if free <= 0:
                break
            if not self.may_call("long", new_seat=bid not in asked):
                continue
            if not self.machine_ok():
                break
            n = 1 + sum(e["type"] == "sv_seat" and e.get("batch") == bid for e in self.intents.values())
            if self.start_job("seat", f"{bid}:{n}", fm("seat", bid), timeout_s=self.job_timeout(bid, "seat"),
                              batch=bid):
                free -= 1
                busy.add(bid)

    def deliver(self, *, retries_only=False):
        """§2.5: pending inbox messages go to an idle session of an `auto` batch through its carrier (a busy one, or
        watch / accompany, gets them from its Stop hook at the end of the turn). A delivery starts a model turn, so
        only with quota known and not exhausted. `retries_only` (a full block): only to a holder with an API-error
        retry pending (stuck.py). REQ-18: first the tools left open by the holder of any unfinished batch are
        cleared as stuck.check does for the session it watches (stuck.clear_tool); REQ-17: a run of API errors its
        holder or pending successor got retries for is closed by a good reply (stuck.retry_ended)."""
        seen = set()
        for bid in self.order:
            if self.state(bid) in ready.FINISHED:
                continue
            if (h := self.holders.get(bid)) and h != lock.USER:
                with self.guard("tool_open", h):
                    stuck.clear_tool(self, bid, h, heartbeat.read(self.root, h) or {})
            for s in {self.holders.get(bid), self.pending_successor(bid)} - {None, lock.USER}:
                with self.guard("api_retry_result", s):
                    stuck.retry_ended(self, bid, s)
            if self.headers[bid].get("mode") != "auto":
                continue
            for s in (self.holders.get(bid), self.pending_successor(bid)):  # the latter: its stuck reminders
                if s and s != lock.USER and s not in seen:
                    seen.add(s)
                    with self.guard("deliver", s):
                        self._deliver(bid, s, retries_only)

    def idle(self, car, s, hb, *, since=None) -> bool:
        """finding 18: the screen's tui-idle can miss an idle agent; a turn that ended (last hook Stop) is idle too.
        Sent to a busy agent after all, a delivery waits in its input queue: delivered once, just later. With
        `since` (epoch) such a Stop counts only when later (hb ts is cut to the second: it only ever looks earlier)."""
        st = car.read_state(s)
        return st.alive and (st.idle or hb.get("event") == "Stop" and (since is None or _epoch(hb.get("ts")) > since))

    def delivered_at(self, s) -> float:
        """When s's inbox cursor last moved (the Stop hook's block, _deliver): the Stop before it let the turn go on
        to read what came, so it is no end of the work (r1)."""
        with contextlib.suppress(FileNotFoundError):
            return inbox._paths(s, self.root)[1].stat().st_mtime
        return 0.0

    def _deliver(self, bid, s, retries_only=False):
        hb = heartbeat.read(self.root, s) or {}

        def pending():
            # §6.4: a session handing off gets only the API-error retries its inbox starts with, while its transcript
            # still ends on a retryable error (m2c.2, Q-25), and nothing once it wrote its section (r3: the successor
            # verifies against it); `handoff --accept` forwards the rest to the successor. A full block: the whole
            # inbox, only with a retry in it and the same transcript check (r4: resuming the holder's turn)
            if hb.get("handoff_requested") and self.handed_off(bid, s, hb.get("handoff_requested_at")):
                return []
            msgs = inbox.pending_messages(s, root=self.root)
            if hb.get("handoff_requested"):
                msgs = stuck.retries_first(self, msgs)
            elif not retries_only:
                return msgs
            elif not any(m.sender in stuck.retry_senders(self) for m in msgs):
                return []
            if msgs and not (stuck.api_error(self, s, hb) or {}).get("retryable"):
                return []
            return msgs

        if hb.get("tool_open") or not pending():
            return
        car = self.carrier_for(bid, s)
        if not self.idle(car, s, hb) or not self.may_call("long"):
            return
        with inbox.locked(s, root=self.root):
            msgs = pending()
            if not msgs:
                return
            upto = msgs[-1].end
            k = 1 + sum(e["type"] == "sv_deliver" and e.get("session") == s and e.get("upto") == upto
                        for e in self.intents.values())
            did = f"sv_deliver:{s}:{upto}:{k}"
            self.emit("sv_deliver", phase="intent", dedupe_id=did, session=s, upto=upto, batch=bid)
            try:
                car.deliver(s, "收件箱新消息（按顺序处理）：\n\n" + "\n\n".join(f"［{m.sender}］{m.text}" for m in msgs))
            except self.ERRORS as e:
                self.emit("sv_deliver", dedupe_id=did, session=s, ok=False, error=str(e))
                return
            inbox.mark_delivered(s, upto, root=self.root)
            self.emit("sv_deliver", dedupe_id=did, session=s, ok=True)
        self.did.append(f"{s}: delivered {len(msgs)} message(s)")
