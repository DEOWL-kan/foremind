"""`foremind tick | supervise | pause | resume | run <batch…> | confirm-exit <batch>` (DESIGN §1.1, §10.1, §10.2,
§20 I52③). The supervisor's successor job runs this module itself (`python -m foremind.commands.supervise <batch>`:
open_seat(successor=True)), so it is not a `foremind` command.

The commands that run a pass make git and ssh non-interactive (SF-4): GIT_SSH_COMMAND becomes the user's own ssh
command (GIT_SSH_COMMAND, else `git config --global --includes core.sshCommand`, else GIT_SSH, else `ssh`) plus
`-o BatchMode=yes`. A repository-level core.sshCommand is overridden by it (the environment wins over git config):
put that one in the global config or the environment. So is a per-directory one under `includeIf`: the one
matching where the pass runs (if any) is used for every repository. `confirm-exit` is the user's: refused inside a Foremind
session (§20 I52③)."""
import contextlib
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime

from foremind import carriers, cli, config, lock, review, seat, worktree
from foremind import notify as notifier
from foremind.defaults import TABLE
from foremind.events import EventLog
from foremind.fsutil import atomic_write
from foremind.paths import ProjectNotFound, find_project_root, state_dir
from foremind.supervisor import tick as sv

_ERRORS = (OSError, ProjectNotFound, ValueError, config.ConfigError, seat.SeatError)
TRY = ("supervise", "--help")  # _reexec's try run


def register(sub):
    sub.add_parser("tick", help="one supervisor pass (what `supervise` repeats)").set_defaults(func=_tick)
    sub.add_parser("supervise", help="run the supervisor in the foreground, a tick every supervisor.tick_s "
                                     "seconds").set_defaults(func=_supervise)
    sub.add_parser("pause", help="pause every automatic action of this project").set_defaults(func=_pause, on=True)
    sub.add_parser("resume", help="undo `foremind pause`").set_defaults(func=_pause, on=False)
    p = sub.add_parser("run", help="start these batches now (dependencies, decisions, quota, locks still apply)")
    p.add_argument("batches", nargs="+")
    p.set_defaults(func=_run)
    p = sub.add_parser("confirm-exit", help="confirm that the session the supervisor could not close has exited")
    p.add_argument("batch")
    p.set_defaults(func=_confirm_exit)


def _fail(name, e) -> int:
    print(f"foremind {name}: {e}", file=sys.stderr)
    return 1


def _ssh_command() -> str:
    base = os.environ.get("GIT_SSH_COMMAND")
    if not base:
        try:
            base = subprocess.run(["git", "config", "--global", "--includes", "--get", "core.sshCommand"],
                                  capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            base = ""
    if not base and os.environ.get("GIT_SSH"):
        base = shlex.quote(os.environ["GIT_SSH"])  # a program path, not a command line
    return (base or "ssh") + " -o BatchMode=yes"


def _unattended():
    """SF-4: nothing a pass runs itself (git, ssh, gh) waits for a person, and each git / gh call through review.run
    is bounded: the pass holds the global lock meanwhile. Jobs have their own stdin and time limits (job.start)."""
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    os.environ["GIT_SSH_COMMAND"] = _ssh_command()
    os.environ["GH_PROMPT_DISABLED"] = "1"
    fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(fd, 0)
    os.close(fd)
    review.TIMEOUT_S = sv.NET_TIMEOUT_S


def _tick(args):
    _unattended()
    try:
        return sv.tick()
    except _ERRORS as e:
        return _fail("tick", e)


def _supervise(args):
    """Finding 23: records itself in supervisor.json; once the package's code changed and then held still for one
    interval, tries the new code (`foremind supervise --help`, 30 s: it imports this module and the tick; the cli
    skips a module that does not import) and re-executes itself on it (`supervisor_reexec`), else keeps the old code
    (`supervisor_reexec_failed`, one notice per new fingerprint)."""
    try:
        root = find_project_root()
    except ProjectNotFound as e:
        return _fail("supervise", e)
    env = dict(os.environ)  # as started: _unattended adds to GIT_SSH_COMMAND
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(filter(None, [str(sv.PKG_PARENT),
                                                                    *env.get("PYTHONPATH", "").split(os.pathsep)])))
    argv = sv.fm("supervise")
    _unattended()
    code = seen = sv.code_fingerprint()
    failed = None
    try:
        atomic_write(sv.supervisor_path(root), json.dumps({
            "pid": os.getpid(), "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "code": code, "argv": argv}))
    except OSError as e:
        return _fail("supervise", e)
    try:
        while True:
            interval = TABLE["supervisor.tick_s"]
            try:
                if (now := sv.code_fingerprint()) != code and now == seen and now != failed:  # after each sleep
                    failed = now
                    _reexec(root, argv, env, code, now)  # returns only when the new code does not run
                seen = now
                sv.tick(root)
                interval = sv.setting(config.load(root), "supervisor.tick_s")
            except Exception as e:  # SF-3: one bad pass does not stop the loop
                print(f"foremind supervise: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def _reexec(root, argv, env, frm, to):
    log = EventLog(state_dir(root) / "events.jsonl")
    try:
        r = subprocess.run(sv.fm(*TRY), env=env, cwd=root, capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        lines = r.stderr.strip().splitlines() or [""]  # the cli's skip line says more than argparse's invalid choice
        err = None if r.returncode == 0 else (next((x for x in lines if x.startswith(cli.SKIPPED)), lines[-1])
                                              or f"exit {r.returncode}")
    except (OSError, subprocess.SubprocessError) as e:
        err = str(e)
    if err is None:
        log.append("supervisor_reexec", **{"from": frm, "to": to})
        print(f"foremind supervise: new code ({to}), re-executing", file=sys.stderr)
        sys.stdout.flush()  # a pipe's buffer does not survive exec
        try:
            os.execve(sys.executable, argv, env)
        except OSError as e:
            err = f"exec: {e}"
    log.append("supervisor_reexec_failed", **{"from": frm, "to": to}, error=err[-300:])
    print(f"foremind supervise: new code does not run, keeping the old one: {err}", file=sys.stderr)
    with contextlib.suppress(config.ConfigError, ValueError):  # a bad config: the tick records it
        notifier.notify(root, config.load(root), f"supervisor_reexec_failed:{to}", "监督进程没能换上新代码",
                        "新代码试跑失败，监督进程继续用旧代码；修好后它会再试。foremind doctor 可查看。")


def _pause(args):
    try:
        changed = sv.pause(find_project_root(), args.on)
    except _ERRORS as e:
        return _fail("pause" if args.on else "resume", e)
    print(("paused" if args.on else "resumed") + ("" if changed else " already"))
    return 0


def _run(args):
    _unattended()
    try:
        root = find_project_root()
        states = {b: seat.read_header(root, b).get("state") for b in args.batches}  # every id known before a move
        for b in args.batches:  # m2b.6: a failed batch is retried from ready
            if states[b] == "failed":
                review.set_state(root, b, "ready", expect=("failed",))
                review.events(root).append("batch_retried", batch=b, by=os.environ.get("FOREMIND_SESSION") or "user")
        sv.request_run(root, args.batches)
        return sv.tick(root)
    except (*_ERRORS, review.FlowError) as e:
        return _fail("run", e)


def _confirm_exit(args):
    if os.environ.get("FOREMIND_SESSION"):  # M-1: a session must not vouch for another session's exit
        return _fail("confirm-exit", "only the user confirms an exit, not a Foremind session")
    try:
        root = find_project_root()
        s = sv.confirm_exit(root, args.batch)
    except _ERRORS as e:
        return _fail("confirm-exit", e)
    print(f"{args.batch}: exit of {s} confirmed")
    _unattended()
    try:
        return sv.tick(root)
    except _ERRORS as e:
        return _fail("confirm-exit", e)


def _successor(batch) -> int:
    try:
        root = find_project_root()
        cfg = config.load(root, config.task_layer(seat.read_header(root, batch)))  # as `foremind seat` does
        res = seat.open_seat(root, batch, carrier=carriers.get(seat.setting(cfg, "carrier.kind"), root, cfg),
                             successor=True)
    except (*_ERRORS, lock.LockError, worktree.WorktreeError, carriers.CarrierError, NotImplementedError) as e:
        return _fail("successor", e)
    if not res["ok"]:
        return _fail("successor", f"{batch}: " + "; ".join(res["problems"]))
    print(f"{res['session']}\t" + "\t".join(f"{rid}={p}" for rid, p in res["worktrees"].items()))
    return 0


if __name__ == "__main__":
    sys.exit(_successor(sys.argv[1]))
