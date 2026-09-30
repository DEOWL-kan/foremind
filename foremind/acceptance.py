"""Acceptance commands and [gate].checks on the batch heads (DESIGN §7.2, §20 I8).

Results go to batches/<id>.accept.<heads-hash>.json (and .checks.<heads-hash>.json for [gate].checks), schema
acceptance_result, each announced by an `<kind>_run` event carrying the file's sha256. A rerun on the same heads
overwrites the file, so the latest run counts: red then green on one SHA passes, green then red fails.
Commands never see the seat's live worktree (§20 I50): each run checks the heads out detached, hooks off
(review.checkout), into a throwaway directory laid out like the batch directory (<tmp>/<repo>) and deletes it
afterwards. The checkout holds tracked files only: commands prepare their own dependencies. Each command
runs as `sh -c` through job.start (timeout kills the whole group). A command is a string or {run, repo}:
single-repo batches run in that repo's checkout, multi-repo batches in the directory holding the checkouts, and a
command with `repo` in that repo's checkout. Each result entry records that repo (none for the batch directory), so
the gate binds a result to (repo, command), not the command text alone.
"""
import json
import shutil
import tempfile
import time
from pathlib import Path

from foremind import job, schemas
from foremind.defaults import TABLE
from foremind.fsutil import atomic_write, sha256_bytes
from foremind.paths import state_dir
from foremind.review import FlowError, batch_repos, checkout, drop_checkouts, events, heads, heads_hash, load_batch


def commands(h, cfg, kind) -> list:
    return h["accept_commands"] if kind == "accept" else cfg.get("gate.checks", TABLE["gate.checks"])


def command_text(c):
    return c if isinstance(c, str) else c.get("run") if isinstance(c, dict) else None


def command_repo(c, ids):
    """Repo the command runs in: its `repo`, else the only repo of a single-repo batch; None = the directory holding
    a multi-repo batch's checkouts."""
    rid = c.get("repo") if isinstance(c, dict) else None
    return rid if rid is not None else ids[0] if len(ids) == 1 else None


def result_path(root, batch_id, kind, hd):
    return state_dir(root) / "batches" / f"{batch_id}.{kind}.{heads_hash(hd)}.json"


def wait(jobs, job_id) -> dict:
    while (st := job.status(jobs, job_id))["state"] in ("starting", "running"):
        time.sleep(0.05)
    return st


def run(root, batch_id, cfg, kind="accept") -> dict:
    h = load_batch(root, batch_id)
    pairs = batch_repos(root, h, cfg)
    cmds = commands(h, cfg, kind)
    if not cmds:
        raise FlowError(f"{batch_id}: no {kind} commands")
    ids = [r.id for r, _ in pairs]
    todo = [(command_text(c), command_repo(c, ids)) for c in cmds]
    for c, (text, rid) in zip(cmds, todo):
        if not isinstance(text, str) or not text.strip() or (rid is not None and rid not in ids):
            raise FlowError(f"{batch_id}: bad {kind} command {c!r}")
    hd = heads(pairs)
    jobs = state_dir(root) / "jobs"
    timeout = int(cfg.get("acceptance.timeout_min", TABLE["acceptance.timeout_min"])) * 60
    tmp = Path(tempfile.mkdtemp(prefix=f"foremind-{batch_id}-{kind}-"))
    out = []
    try:
        checkout(pairs, hd, tmp)
        for text, rid in todo:
            jid = job.start(jobs, ["/bin/sh", "-c", text], cwd=tmp / rid if rid else tmp, timeout_s=timeout)
            code = wait(jobs, jid).get("exit_code", -1)  # a lost runner has no exit code
            out.append({"command": text, **({"repo": rid} if rid else {}), "exit_code": code,
                        "output_path": str(jobs / jid / "stdout.log")})
    finally:
        drop_checkouts(pairs, tmp)
        shutil.rmtree(tmp, ignore_errors=True)
    res = {"batch": batch_id, "heads": hd, "commands": out}
    if errs := schemas.validate("acceptance_result", res):
        raise FlowError(f"{batch_id}: {kind} result fails schema: {'; '.join(errs[:3])}")
    data = json.dumps(res, ensure_ascii=False, indent=2) + "\n"
    path = result_path(root, batch_id, kind, hd)
    atomic_write(path, data)
    events(root).append(f"{kind}_run", batch=batch_id, heads=hd, path=path.name, sha256=sha256_bytes(data.encode()),
                        ok=all(c["exit_code"] == 0 for c in out))
    return res
