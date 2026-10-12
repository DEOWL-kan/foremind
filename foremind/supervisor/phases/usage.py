"""Usage at merge (m2f REQ-12): a batch merged or cataloged with no batch_usage event yet gets one,
batch_usage{batch, writer, reviewer} (metrics.usage_of: {session: controller.usage or null}), so the report no longer
needs its transcripts still there. At most PER_PASS batches a pass, in batch-number order; also under a full block
(it starts no model and changes no state). The pass after a batch became merged covers land, reconcile and merge_dev.
"""
import re

from foremind import metrics

PER_PASS = 5
DONE = ("merged", "cataloged")


def _num(bid) -> list:
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", bid)]


def run(t, blocked):
    # ponytail: the transcripts are read inside the tick's global lock; the first pass after this lands has ~50 merged
    # batches to catch up on, 10-odd passes at PER_PASS each; raise PER_PASS or read outside the lock if a pass drags
    todo = [b for b in sorted(t.order, key=_num) if t.state(b) in DONE and f"batch_usage:{b}" not in t.results]
    by = metrics.group(t.evs) if todo else {}
    for bid in todo[:PER_PASS]:
        with t.guard("usage", bid):
            u = metrics.usage_of(t.root, by.get(bid, []))
            t.emit("batch_usage", dedupe_id=f"batch_usage:{bid}", batch=bid, **u)
