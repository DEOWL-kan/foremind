"""`foremind audit` (DESIGN §9.2–9.4, m2b.5); --accept-config and --release are the user's (refused inside a Foremind
session, as `decide`'s answers).

  audit                    the L0 hard failures as of now, and those the tick reported that no pass has cleared since
                           (audit.uncleared: tick_crash, config_invalid, events_invalid, l0_error), reported ones
                           marked; the pre-delivery audits held; and every bounds_violation / bounds_error
                           (phases/bounds.py, m2c.6)
  audit --accept-config    record the changed #22 files as `l0_config_accepted {files: [{path, sha256}]}`, then resume;
                           refused while another of those is unaccepted or uncleared (resume would accept it unseen)
  audit --release <batch>  awaiting_audit -> delivered (batch_state with via), then the gate as `foremind gate <batch>`
"""
import argparse
import os
import sys

from foremind import audit, config, review
from foremind.commands import gate as gate_cmd
from foremind.events import EventLog
from foremind.paths import ProjectNotFound, find_project_root, state_dir
from foremind.supervisor import tick


def register(sub):
    p = sub.add_parser("audit", help="L0 findings and held audits; accept config changes or release a held batch")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--accept-config", action="store_true", help="accept the changed #22 files and resume")
    g.add_argument("--release", metavar="BATCH", help="release a batch held in awaiting_audit, then run its gate")
    p.set_defaults(func=_run)


def _run(args):
    try:
        if (args.accept_config or args.release) and os.environ.get("FOREMIND_SESSION"):
            raise ValueError("--accept-config 与 --release 只归用户，Foremind 会话不能执行")
        if args.release:
            root, _ = review.context(args.release)
            review.set_state(root, args.release, "delivered", expect=("awaiting_audit",), via="audit --release")
            print(f"{args.release}: released to delivered; running its gate")
            return gate_cmd._run(argparse.Namespace(batch=args.release, rerun=False, no_merge=False))
        root = find_project_root()
        cfg = config.load(root)
        fails = audit.l0(root, cfg)[2]
        evs, have = tick._events(root), {(f["check"], f["target"]) for f in fails}
        fails += [f for f in audit.uncleared(evs) if (f["check"], f["target"]) not in have]  # the tick's (r9)
        if args.accept_config:
            if other := [f for f in fails if f["check"] != "config"]:  # resuming would accept these as well
                raise ValueError("还有配置文件以外的问题，核实后执行 foremind resume（连同配置变化一并接受）：\n"
                                 + "\n".join(f"L0 {f['check']} {f['target']}: {f['detail']}" for f in other))
            files = [{"path": f["target"], "sha256": f["sha256"]} for f in fails if f["check"] == "config"]
            for f in files:
                print(f"accepted {f['path']} ({'deleted' if f['sha256'] is None else f['sha256'][:12]})")
            if files:
                EventLog(state_dir(root) / "events.jsonl").append("l0_config_accepted", files=files)
            print("resumed" if tick.pause(root, False) else "not paused")
            return 0
        _list(root, evs, fails)
        return 0
    except (ValueError, OSError, review.FlowError, config.ConfigError, ProjectNotFound) as e:
        print(f"foremind audit: {e}", file=sys.stderr)
        return 2


def _list(root, evs, fails):
    """Pre-delivery holds only while the batch still awaits that audit; a decider sample's P0/P1 stay listed."""
    seen, lines = audit.reported(evs), []
    for f in fails:
        lines.append(f"L0 {f['check']} {f['target']}: {f['detail']}" + ("（已报告）" if f["fingerprint"] in seen else ""))
    reports = {e.get("key"): e.get("report") for e in evs if e["type"] == "audit_done"}
    for e in evs:
        if e["type"] != "audit_held":
            continue
        if e.get("trigger") == "pre_delivery":
            try:
                if review.load_batch(root, e["target"]).get("state") != "awaiting_audit" or \
                        audit.pre_key(evs, e["target"]) != e["key"]:
                    continue
            except review.FlowError:
                continue
        rep = reports.get(e["key"])
        lines.append(f"{e['target']} ({e['trigger']}): {e['why']}"
                     + (f" · {state_dir(root) / 'reports' / rep}" if rep else "")
                     + (f" · foremind audit --release {e['target']}" if e["trigger"] == "pre_delivery" else ""))
    for e in evs:
        if e["type"] == "bounds_violation":
            what = [f"{x.get('path')}（{x.get('why')}）" for x in e.get("paths") or []] + [
                f"{x.get('repo')} {x.get('branch')}：远端 {'、'.join(map(str, x.get('on_remote') or [])) or '-'}"
                f" PR {'、'.join(f'#{n}' for n in x.get('prs') or []) or '-'}"
                for x in e.get("repos") or []]
            lines.append(f"越界 {e.get('batch')} ({e.get('kind')}) {e['ts']}: " + "；".join(what))
        elif e["type"] == "bounds_error":
            lines.append(f"越界核对出错 {e.get('batch')} ({e.get('kind')}) {e['ts']}: {e.get('error')}")
    print("\n".join(lines) or "nothing to report")
