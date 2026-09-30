"""Phase: run report and failed batches (m2b.6, DESIGN §11.3, §20 I64). Calls no model, so it runs under a full block
too.

- A batch the event log shows moved to `failed` (review.py: review_failures, review_timeouts) is told once per that
  batch_state event, at P1: retry with `foremind run <batch>`; for review_timeouts with the count and what to change
  first (the only notice of it, m2c.7).
- A stretch of unattended running has come to a rest when every unfinished batch waits on the user (blocked: the
  tick's full block, `t.block` from announce_block) or none is unfinished. Then one report (report.generate) and one
  push of its summary (ids and counts, §8.7), once per fingerprint: under a full block that of its reasons, as
  announce_block did (the push is the one full-block notice, so it is sent even with no batch change since the last
  report; a fingerprint announce_block already pushed as full_block:<fp> is not pushed again); all finished, that of
  the finished batches, and only when a batch moved (report.moves) after the last report. The push goes before the
  report's report_generated, which marks the fingerprint done: a crash between leaves the report to the next pass,
  whose push notify() dedupes; a push notify() did not handle (an invalid notify.channel) leaves no report either, so
  the next pass pushes again, as announce_block did.
- Under a pause no pass runs: paused(), from the tick's paused branch, carries out the P0s the limit held.
"""
import json

from foremind import notify as notifier
from foremind import report
from foremind.fsutil import sha256_bytes
from foremind.notify import REPORT_KEY
from foremind.supervisor import ready


def run(t, blocked):
    with t.guard("report", "failed"):
        failed(t)
    done = t.headers and all(t.state(b) in ready.FINISHED for b in t.headers)
    if not (blocked or done):  # work goes on: no report due, the history (archives too) is not read (m2d.10 r1)
        return
    evs = report.events(t.root)  # fresh: this pass has written some
    if blocked:
        fp, block = t.block
        key, title = f"{REPORT_KEY}block:{fp}", "全部批次都在等你"
        if any(e["dedupe_id"] == f"notify:full_block:{fp}" for e in evs):
            return
    else:
        fp, block = sha256_bytes(json.dumps(sorted(t.headers)).encode())[:16], ()
        key, title = f"{REPORT_KEY}done:{fp}", "本段自动运行已结束"
        if not report.moves(report.after(evs, report.last(evs))):
            return
    if any(e["type"] == "report_generated" and e["dedupe_id"] == key for e in evs):
        return
    f = report.facts(t.root, report.last(evs), t.now, t.cfg)
    lead = () if blocked else (f"状态变化 {report._n(f['changes'])}",)
    t.notify(key, title, "\n".join(x for x in (*block, *lead, report.summary(f)) if x))
    if not any(e["dedupe_id"] == f"notify:{key}" for e in report.events(t.root)):
        return
    path, _, _ = report.generate(t.root, report.last(evs), t.now, block, dedupe_id=key, f=f)
    t.did.append(f"run report {path}")


def paused(root, cfg) -> str | None:
    """REQ-16 under a pause (controller r2 ruling): no pass runs, so a P0 the limit held (an L0 pause's own among them)
    would meet no later P0 or run report. Once per set of held P0s not carried out (after the last report and the last
    P0 sent): a run report and one push of its summary (REPORT_KEY, P1: neither held nor deferred), on `cfg`, the user
    layer's as for the L0 P0. A notice, no action: no seat, gate or batch move. The report's path, or None."""
    evs = report.events(root)
    last = report.last(evs)
    since = {e["id"] for e in report.after(evs, last)}
    held = [e for e in notifier.held(evs) if e["id"] in since]
    if not held:
        return None
    key = f"{REPORT_KEY}paused:{sha256_bytes(json.dumps(sorted(e['key'] for e in held)).encode())[:16]}"
    if any(e["type"] == "report_generated" and e["dedupe_id"] == key for e in evs):
        return None
    f = report.facts(root, last)  # not `cfg`: the user layer only, the suggestions compare with the project's
    notifier.notify(root, cfg, key, "已暂停：有告警被限流", "\n".join(
        [f"项目已暂停；另有 {len(held)} 条告警未单独推送：", *(f"- {e.get('title')}" for e in held), report.summary(f)]))
    return report.generate(root, last, dedupe_id=key, f=f)[0]


def failed(t):
    for bid in t.order:
        if t.state(bid) != "failed":
            continue
        e = next((e for e in reversed(t.evs) if e["type"] == "batch_state" and e.get("batch") == bid
                  and report._to(e) == "failed"), None)
        if e is None:  # no move recorded: nothing new to tell
            continue
        why = f"（{e['reason']}）" if e.get("reason") else ""
        if e.get("reason") == "review_timeouts":  # the one notice of it (m2c.7, m2b.6 r3): update.py told it too
            why += (f"：审查在同一组 heads 上连续 {e.get('failures')} 次超时，已停止重跑；"
                    "调大 oneshot.timeout_min 或缩小改动后")
        t.notify(f"failed:{bid}:{e['id']}", f"{bid} 连续失败已停",
                 f"{bid} 连续失败已停{why}，用 foremind run {bid} 重试。")
