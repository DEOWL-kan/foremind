"""`foremind tick | supervise | pause | resume | run <batch…> | confirm-exit <batch>` (DESIGN §1.1, §10.1, §10.2,
§20 I52③). The supervisor's successor job runs this module itself (`python -m foremind.commands.supervise <batch>`:
open_seat(successor=True)), so it is not a `foremind` command.

The commands that run a pass make git and ssh non-interactive (SF-4): GIT_SSH_COMMAND becomes the user's own ssh
command (GIT_SSH_COMMAND, else `git config --global --includes core.sshCommand`, else GIT_SSH, else `ssh`) plus
`-o BatchMode=yes`. A repository-level core.sshCommand is overridden by it (the environment wins over git config):
put that one in the global config or the environment. So is a per-directory one under `includeIf`: the one
matching where the pass runs (if any) is used for every repository. `confirm-exit` is the user's: refused inside a Foremind
session (§20 I52③)."""
import os
import shlex
import subprocess
import sys
import time

from foremind import carriers, config, lock, review, seat, worktree
from foremind.paths import ProjectNotFound, find_project_root
from foremind.supervisor import tick as sv

_ERRORS = (OSError, ProjectNotFound, ValueError, config.ConfigError, seat.SeatError)


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
    try:
        root = find_project_root()
    except ProjectNotFound as e:
        return _fail("supervise", e)
    _unattended()
    try:
        while True:
            interval = sv.DEFAULTS["supervisor.tick_s"]
            try:
                sv.tick(root)
                interval = sv.setting(config.load(root), "supervisor.tick_s")
            except Exception as e:  # SF-3: one bad pass does not stop the loop
                print(f"foremind supervise: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


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
        sv.request_run(root, args.batches)
        return sv.tick(root)
    except _ERRORS as e:
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
