"""One supervisor pass (DESIGN §1.1, §10.2): `tick(project, now)`; plus `pause`, `request_run` and `confirm_exit`.

The global lock `<FOREMIND_CONFIG_HOME>/supervisor.lock` is taken without waiting: held elsewhere -> one line, exit 0.
Project root `STOP` or `.foremind/paused` -> no automatic action at all (running sessions are left alone). Otherwise:
  1  read plans, batch headers, decisions/Q-*.json, quota telemetry, and events with locks in one project-lock step
  2  minimal L0 (§20 I2, I52①): every started batch that gate.batch_merged finds merged is caught up to `merged`
     (event `reconciled` with `skipped`), each batch checked at most every supervisor.merged_check_min (fetch and gh
     run under the global lock); skipping approved or delivered, or awaiting_audit when the tier audits before
     delivery, is a hard failure: event `l0_hard_failure` (cataloger_hold) + P0 notice
  3  finish actions that have an intent and no result by checking the fact per kind, never by redoing them: a seat
     or successor job -> the carrier and `seat_opened`, read again once the job is seen ended (a launch that never
     kicked off is closed, its claim given back only with exit evidence; the same for a seat_open / seat_continue
     of no job of ours once past the seat job time limit); a delivery -> the inbox cursor and `program_delivery`; an
     inbox message -> the inbox file; a notification -> our own record (whether it went out is unknown: recorded,
     plus `notify_unsent` with unknown=true for the morning report, never resent)
  4  quota per account x role group (quota.py), then the ready queue (ready.py): planned -> ready
  5  sessions to be rid of, no model call: those left on finished batches, a stuck batch's stuck session, the claim
     of a seat that never kicked off, a successor that can no longer accept (closing retried with backoff; under
     manual, or once the backoff is at 30 minutes, the user answers with `foremind confirm-exit`); with exit
     evidence their locks are broken
  6  unless every unfinished batch waits on the user (full block: no model call this tick, one summary notice per
     set of reasons): successors for started batches nobody works on (never while one opened before has not run
     `handoff --accept`: that one is watched for being stuck instead), stuck seats (stuck.py), gate runs after an
     approved receipt (retried while approved, or delivered at merge_dev up to supervisor.seat_retries failures),
     must-fix lists (changes_requested -> running), reviewers (review.start), new seats (`foremind seat`),
     "continue" after quota exhaustion (one session per tick), inbox delivery to idle auto sessions, then one quota
     probe per account (the reviewer's model, exclusions checked) if any of these wanted a model while quota was
     unknown
Every action writes an intent first and a result after; long ones (seat, successor, gate, probe) run as detached
jobs (job.start, with a time limit: event `sv_<kind>_timed_out`, a probe's `oneshot_interrupted`) harvested by a
later tick, so the lock is never held while they run. One bad
file costs its own item a `tick_error`, not the pass. Nothing here asks a model to judge. Times are epoch seconds;
`now` can be injected.
"""
import contextlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from foremind import carriers, config, gate, handoff, header, heartbeat, inbox, job, lock, review, seat, worktree
from foremind import notify as notifier
from foremind.events import EventLog
from foremind.fsutil import LockBusy, atomic_write, global_lock, project_lock, sha256_bytes
from foremind.paths import find_project_root, state_dir
from foremind.plan import model
from foremind.state import BATCH, BATCH_SIDE, reconcile
from foremind.supervisor import quota, ready, stuck
from foremind.vendors import claude

# jobs run in the project root: -P and the hooks' PYTHONPATH keep a `foremind/` there (foremind's own repo) from
# standing in for this package (§20 I53①)
PKG_PARENT = claude._PKG_PARENT  # the same path the hooks carry
FM = [sys.executable, "-P", "-m", "foremind"]
SUCCESSOR = [sys.executable, "-P", "-m", "foremind.commands.supervise"]  # + batch: out of `foremind`'s command list
DEFAULTS = {"supervisor.tick_s": 30, "supervisor.max_seats": 2, "supervisor.max_oneshot": 2,
            "supervisor.seat_retries": 2, "supervisor.gate_retry_min": 10, "supervisor.merged_check_min": 5,
            "oneshot.timeout_min": 30}
NET_TIMEOUT_S = 120  # each git / gh call the pass makes itself through review.run (SF-4; set by the commands)
JOB_SLACK_S = 900  # on top of a job's own command timeouts: fetches, worktrees, gh
LIVE = ("running", "changes_requested")  # a seat is working on these
WAIT_TEXT = {"watch": "等你在场（或 foremind run）", "accompany": "等你在场（或 foremind run）",
             "user": "等你认领（foremind seat --user）"}
LOW_TEXT = "Foremind：额度偏低。到下一个安全点写交接段（`foremind handoff --write`），写完可以继续。"
RESUME_TEXT = "Foremind：额度已恢复，继续。"
GATE_TEXT = "Foremind：门禁未通过：\n{fails}\n处理后提交，再执行 `foremind review`。"
ERRORS = (OSError, ValueError, config.ConfigError, carriers.CarrierError, lock.LockError, review.FlowError,
          seat.SeatError, handoff.HandoffError, worktree.WorktreeError, inbox.InboxCorrupt, NotImplementedError)


def setting(cfg, key):
    v = cfg.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else DEFAULTS[key]


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
        return 0
    try:
        cfg = config.load(root)
    except config.ConfigError as e:  # §20 I17
        EventLog(state_dir(root) / "events.jsonl").append(
            "config_error", dedupe_id=f"config_error:{sha256_bytes(str(e).encode())[:16]}", error=str(e))
        print(f"foremind tick: {e}", file=sys.stderr)
        return 1
    t = Tick(root, cfg, now)
    t.run()
    for line in t.did:
        print(line)
    return 0


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
        self.did, self.demand, self.evs, self._cfgs = [], set(), [], {}
        self._index()
        # ponytail: quota state per project; it is per account, so it moves to the user level with M2-8 (the
        # merged-check times in it stay per project)
        self.qpath = self.sd / "quota.json"
        try:
            self.qfile = json.loads(self.qpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.qfile = {}
        self.qdirty = False

    def run(self):
        self.load()
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
        direct, why = self.waits()
        blocked = ready.full_block(self.headers, direct, why)
        if blocked is not None:
            self.announce_block(blocked)
        else:
            self.seats_recover()
            for bid in self.order:
                if self.state(bid) == "running":
                    with self.guard("stuck", bid):
                        stuck.check(self, bid)
            self.gates()
            self.start_reviews()
            self.open_seats()
            self.resume_one()
            self.deliver()
            self.probes()  # last: every wish to call made this pass counts
        if self.qdirty:
            self.save_quota()

    def save_quota(self):
        atomic_write(self.qpath, json.dumps(self.qfile, indent=1, sort_keys=True))
        self.qdirty = False

    # --- state -----------------------------------------------------------------

    # ponytail: every pass loads plans, headers and the whole event log 3-4 times, and pending_successor, _opened,
    # failures, close_due scan all events per batch (O(batches x events)); index the events once per load if a
    # project's log grows past a few thousand events
    def load(self):
        self.plans, self.headers, self.plan_of, self.bound, self._cfgs = {}, {}, {}, {}, {}
        for pid in model.plan_ids(self.root):
            try:
                p = model.load(self.root, pid)
            except (model.PlanError, OSError) as e:
                self.error("plan", pid, e)
                continue
            self.plans[pid], self.bound[pid] = p, model.is_bound(self.root, p)
            for bid, d in p.batches.items():
                self.headers[bid], self.plan_of[bid] = d.header, pid
        self.order = list(self.headers)
        self.decisions = []
        for p in sorted((self.sd / "decisions").glob("Q-*.json")):
            try:
                q = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                self.error("decision", p.name, e)
                continue
            if isinstance(q, dict):
                self.decisions.append(q)
        with project_lock(self.root):  # open_seat claims and writes its seat_open intent in one such step
            self.evs = list(handoff.history(self.root))
            self.holders = {b: lock.holder(self.root, b) for b in self.headers}
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
        return cfg.get("delivery.depends_on", "merged")

    def merge_dev(self, bid) -> bool:
        """Every repo of the batch delivers at merge_dev: the supervisor's gate run merges it (SF-7)."""
        cfg = self.bcfg(bid)
        return cfg is not None and all(review.repo_cfg(cfg, r, "level", "delivery.level", "done") == "merge_dev"
                                       for r in self.headers[bid].get("repos", []))

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

    def opening(self, bid) -> bool:
        """A seat_open or seat_continue of `bid` has begun and not ended (by a job of ours or the user's command)."""
        return bool(self.open_intents("seat_open", batch=bid) or self.open_intents("seat_continue", batch=bid))

    def failures(self, bid, kind, **match) -> int:
        """Failed `sv_<kind>` runs for the batch since it was last opened or asked for with `foremind run`."""
        n = 0
        for e in reversed(self.evs):
            if (e["type"] == "run_requested" and bid in e.get("batches", [])) or \
                    (e["type"] == "seat_opened" and e.get("batch") == bid):
                break
            n += (e["type"] == f"sv_{kind}" and e["phase"] == "result" and e.get("batch") == bid and not e.get("ok")
                  and all(e.get(k) == v for k, v in match.items()))
        return n

    def gave_up(self, bid) -> str | None:
        """Retries used up: seats and successors, or the merge of a delivered merge_dev batch (gate runs while
        delivered); notified once, `foremind run <batch>` starts over."""
        cap = setting(self.cfg, "supervisor.seat_retries")
        kinds = (("seat", "开席", {}), ("successor", "继任", {}))
        if self.state(bid) == "delivered":
            kinds = (("gate", "合入", {"state": "delivered"}),) if self.merge_dev(bid) else ()
        for kind, text, match in kinds:
            if (n := self.failures(bid, kind, **match)) >= cap:
                self.notify(f"{kind}_failed:{bid}:{n}", f"{bid} {text}失败",
                            f"连续 {n} 次没能{text}，已停止自动重试；处理后用 foremind run {bid} 再试。")
                return f"{text}失败 {n} 次"
        return None

    def job_timeout(self, bid, kind) -> int:
        """SF-8: a job that hangs (a fetch, a start or acceptance command) is killed and counts as failed."""
        cfg, h = self.bcfg(bid) or self.cfg, self.headers[bid]
        if kind == "gate":
            n = len(h.get("accept_commands", [])) + len(cfg.get("gate.checks") or [])
            return int(cfg.get("acceptance.timeout_min", 30)) * 60 * max(1, n) + JOB_SLACK_S
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
        if g == "long" and new["state"] == quota.EXHAUSTED:  # these get "continue" one by one on recovery
            self.qfile["paused"] = [s for b, s in self.holders.items() if s and s != lock.USER and self.state(b) in LIVE]
        elif g == "long" and old.get("state") == quota.EXHAUSTED:
            self.qfile["exhausted_ended"] = self.now  # stuck clocks restart here

    def may_call(self, g, *, new_seat=False) -> bool:
        """Low: no new seats (unless `foremind run` asked), half the one-shot concurrency; exhausted: nothing;
        unknown: nothing until a probe succeeds (the wish to call is what triggers the probe)."""
        st = self.q(g)["state"]
        if st == quota.UNKNOWN:
            self.demand.add(g)
        return st == quota.AVAILABLE or (st == quota.LOW and not new_seat)

    def quota(self):
        tel = quota.read_telemetry(self.root)
        for g in quota.GROUPS:
            self.qset(g, quota.step(self.qfile.get("groups", {}).get(f"{quota.ACCOUNT}/{g}"), tel, self.now,
                                    self.cfg, g))
        st = self.q("long")
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
        self.qfile["paused"], self.qdirty = live[1:], True

    def probes(self):
        """One probe for the account, whichever role groups wanted to call while unknown (its answer holds for all)."""
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
        self.start_job("probe", f"{'+'.join(due)}:t{int(self.now)}",
                       ["/bin/sh", "-c", argv] if isinstance(argv, str) else list(argv),
                       timeout_s=setting(self.cfg, "oneshot.timeout_min") * 60, groups=due)

    # --- actions -----------------------------------------------------------------

    def start_job(self, kind, key, argv, *, env=None, timeout_s=None, **fields) -> str | None:
        did = f"sv_{kind}:{key}"
        if did in self.intents:
            return None  # this very action was started before
        self.emit(f"sv_{kind}", phase="intent", dedupe_id=did, **fields)
        path = os.pathsep.join(filter(None, [str(PKG_PARENT), os.environ.get("PYTHONPATH")]))
        # a job is nobody's session: no identity inherited from a Foremind session that ran `foremind run`
        base = {"FOREMIND_SESSION": "", "FOREMIND_BATCH": "", "FOREMIND_ROLE": "",
                "FOREMIND_PROJECT": str(self.root), "PYTHONPATH": path}
        try:
            jid = job.start(self.sd / "jobs", argv, cwd=self.root, timeout_s=timeout_s, env={**base, **(env or {})})
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

    def notify(self, key, title, body, priority="P1"):
        """§20 I52②: title and body carry batch ids, options and a short line, never the project name."""
        try:
            sent = notifier.notify(self.root, self.cfg, key, title, body, priority)
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

    def close(self, bid, session):
        """Close through the carrier: lock.ExitEvidence when the carrier confirms the exit, else None. Every attempt
        is recorded (`sv_close`, `how` when confirmed), so the evidence stands for later passes (exited)."""
        car = ev = err = None
        try:
            car = self.carrier_for(bid, session)
            ev = car.close(session)
        except self.ERRORS as e:
            err = str(e)
            self.did.append(f"error: close {session}: {e}")
        self.emit("sv_close", batch=bid, session=session, carrier=car and car.name, confirmed=ev is not None,
                  **({"how": ev.how} if ev else {}), **({"error": err} if err else {}))
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
        True)); never while one opened before has not accepted (MF-1), nor on a plan changed since approval (SF-9)."""
        if (self.in_flight(bid) or self.pending_successor(bid) or not self.bound[self.plan_of[bid]]
                or self.failures(bid, "successor") >= setting(self.cfg, "supervisor.seat_retries")
                or not self.may_call("long")):
            return
        n = 1 + sum(e["type"] == "sv_successor" and e.get("batch") == bid for e in self.intents.values())
        self.start_job("successor", f"{bid}:{n}", [*SUCCESSOR, bid], timeout_s=self.job_timeout(bid, "seat"),
                       batch=bid, why=why)

    def take_over(self, bid, session):
        """§10.4: close the stuck session and confirm it exited, break its lock (if it holds it) with that evidence,
        open a successor. Without evidence: notify; release() tries again, or the user runs confirm-exit."""
        ev = self.close(bid, session)
        if ev is not None and self.holders.get(bid) == session and not self.break_lock(bid, ev):
            ev = None
        if ev is not None:
            self.successor(bid, "stuck")
        self.notify(f"stuck:{bid}:{session}", f"{bid} 卡住",
                    "已标记 stuck。" + ("旧会话已确认退出，正在开继任会话。" if ev is not None else
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

    def handed_off(self, bid, session) -> bool:
        return any(e["type"] == "handoff_written" and e.get("batch") == bid and e.get("session") == session
                   for e in self.evs)

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
        checked = self.qfile.setdefault("merged_checked", {})
        every = setting(self.cfg, "supervisor.merged_check_min") * 60
        for bid in self.order:
            h = self.headers[bid]
            st = h.get("state", "planned")
            cur = h.get("state_prior") if st in BATCH_SIDE else st
            if cur not in seat.STARTED or self.now - checked.get(bid, 0) < every or (cfg := self.bcfg(bid)) is None:
                continue
            checked[bid] = self.now
            with self.guard("reconcile", bid):
                self.save_quota()  # now: a pass cut short later still keeps this check's limit
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
        for kind in ("seat", "successor", "gate", "probe"):
            for it in self.open_intents(f"sv_{kind}"):
                link = self.result(f"sv_job:{it['dedupe_id']}")
                try:
                    st = job.status(self.sd / "jobs", link["job"]) if link else {"state": "lost"}
                except OSError:
                    st = {"state": "lost"}
                if st["state"] not in ("starting", "running"):
                    ended.append((kind, it["dedupe_id"], link and link["job"], st))
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
                    getattr(self, f"_after_{kind}")(it, ok, st, jid)
                except Exception as e:  # not only ERRORS (a bug, a malformed file): the job waits, the pass goes on
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
        """Settle what the seat job left open."""
        after = self.evs[self.evs.index(it):]
        for e in [e for e in after if e["type"] == "seat_open" and e["phase"] == "intent"
                  and e.get("batch") == it["batch"] and e["dedupe_id"] not in self.results]:
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
        for g in it.get("groups", []):
            self.qset(g, quota.probe_done(self.q(g), ok, self.now, self.cfg, g))
        self.did.append(f"quota: probe {'ok' if ok else 'failed'}")

    def recover(self):
        """Intents left open by a crash in an earlier tick, settled by the facts."""
        for it in self.open_intents("seat_open") + self.open_intents("seat_continue"):
            bid = it.get("batch")
            if bid not in self.headers or self.in_flight(bid):  # a job of ours: its harvest settles it
                continue
            with self.guard("recover", it["dedupe_id"]):
                if self.now - _epoch(it["ts"]) > self.job_timeout(bid, "seat"):  # S-1: e.g. its terminal was closed
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

    def waits(self):
        """({batch: reason} of batches waiting on the user, {batch: why_not_ready} of the other planned/ready)."""
        direct, why = {}, {}
        busy, asked = self.busy(), self.requested()
        for bid in self.order:
            h, st = self.headers[bid], self.state(bid)
            if st in ready.FINISHED:
                continue
            holder, qs, cfg = self.holders.get(bid), ready.blocking(bid, self.decisions), self.bcfg(bid)
            x = self.leftover(bid)
            reason = (f"待决 {'、'.join(qs)}" if qs
                      else "待决" if st == "blocked" and h.get("blocked_reason") == "pending"
                      else "失败，等你决定" if st == "failed"
                      else "等你合入" if st == "delivered" and not self.merge_dev(bid)
                      else "等审计放行" if st == "awaiting_audit"
                      else "你在做" if holder == lock.USER
                      else f"{'卡住' if st == 'stuck' else '开席中断'}，需确认旧会话已退出（foremind confirm-exit {bid}）"
                      if x and self.awaits_confirm(x)
                      else "配置有误" if cfg is None
                      else "计划待批准" if not self.bound[self.plan_of[bid]] and (st in ("planned", "ready")
                                                                              or self.needs_successor(bid))
                      else None)
            if not reason and st in ("planned", "ready") and bid not in asked:
                reason = WAIT_TEXT.get(h.get("mode"))
            if not reason and st in ("ready", "running", "changes_requested", "stuck", "delivered"):
                reason = self.gave_up(bid)
            if reason:
                direct[bid] = reason
            elif st in ("planned", "ready"):
                why[bid] = ready.why_not_ready(bid, self.headers, self.decisions, self.mode(cfg), busy)
        return direct, why

    def announce_block(self, blocked):
        items = sorted(f"{b}：{r}" for b, r in blocked.items())
        fp = sha256_bytes(json.dumps(items, ensure_ascii=False).encode())[:16]
        self.did.append(f"full block: every unfinished batch waits on you ({len(items)}); no model call this tick")
        self.notify(f"full_block:{fp}", "全部批次都在等你", "\n".join(items))

    def seats_recover(self):
        """Started batches nobody works on get a successor (its handoff record, or record "none", said so in the
        kickoff); a claim or stuck session in the way is release()'s."""
        for bid in self.order:
            if self.needs_successor(bid) and not self.gave_up(bid):
                self.successor(bid, "stuck" if self.state(bid) == "stuck" else "no lock holder")

    def gates(self):
        """After an approved receipt the gate runs once (as the supervisor, so a merge_dev batch can merge); an
        approved batch waiting on CI, or a delivered merge_dev one whose merge did not happen (SF-7), is retried every
        supervisor.gate_retry_min, the latter until gave_up. changes_requested -> the must-fix list goes to the
        holder's inbox once per round, and the batch is running again (SF-1: stuck.py watches the session while it
        fixes)."""
        reviewing = {e.get("batch") for e in self.open_intents("review_started")}
        for bid in self.order:
            st = self.state(bid)
            retry = st == "approved" or (st == "delivered" and self.merge_dev(bid) and not self.gave_up(bid))
            if (st not in ("in_review", "changes_requested") and not retry) or bid in reviewing:
                continue
            with self.guard("gates", bid):
                self._gate(bid, st, retry)

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
                lines = [f"- [{i.get('severity')}] {i.get('location')}：{i.get('summary')}" for i in r.get("issues", [])]
                self.say(s, f"Foremind：审查第 {n} 轮要求修改：\n" + "\n".join(lines)
                         + "\n改完提交，再执行 `foremind review`。", key)
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
        n = len(self.open_intents("review_started"))
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

    def open_seats(self):
        in_use = {b for b, s in self.holders.items() if s and s != lock.USER and self.state(b) not in ready.FINISHED}
        cap = setting(self.cfg, "supervisor.max_seats")
        if self.q("long")["state"] == quota.LOW:
            cap = max(1, cap // 2)
        free = cap - len(in_use | {b for b in self.headers if self.in_flight(b) or self.pending_successor(b)})
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
            n = 1 + sum(e["type"] == "sv_seat" and e.get("batch") == bid for e in self.intents.values())
            if self.start_job("seat", f"{bid}:{n}", fm("seat", bid), timeout_s=self.job_timeout(bid, "seat"),
                              batch=bid):
                free -= 1
                busy.add(bid)

    def deliver(self):
        """§2.5: pending inbox messages go to an idle session of an `auto` batch through its carrier (a busy one, or
        watch / accompany, gets them from its Stop hook at the end of the turn). A delivery starts a model turn, so
        only with quota known and not exhausted."""
        seen = set()
        for bid in self.order:
            if self.state(bid) in ready.FINISHED or self.headers[bid].get("mode") != "auto":
                continue
            for s in (self.holders.get(bid), self.pending_successor(bid)):  # the latter: its stuck reminders
                if s and s != lock.USER and s not in seen:
                    seen.add(s)
                    with self.guard("deliver", s):
                        self._deliver(bid, s)

    def _deliver(self, bid, s):
        if not inbox.pending_messages(s, root=self.root) or (heartbeat.read(self.root, s) or {}).get("tool_open"):
            return
        car = self.carrier_for(bid, s)
        st = car.read_state(s)
        if not (st.alive and st.idle) or not self.may_call("long"):
            return
        with inbox.locked(s, root=self.root):
            msgs = inbox.pending_messages(s, root=self.root)
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
