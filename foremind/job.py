"""Detached long actions (DESIGN §1.1): `foremind _job run <spec>` owns the process and writes result files.

result.json `exit_code` follows subprocess: a process killed by a signal has a negative code (-9 = SIGKILL).
The command runs with FOREMIND_JOB=<job id> in its environment, in a session of its own (its process group id is
its pid): when it exits, whatever it left in the group gets SIGTERM. The runner writes its own pid too, so a caller
that dies or fails between Popen and the pid write leaves no job stuck at "starting"; one with no pid _START_S after
its spec was written never started and is "lost".
Caveats: env values are passed through str(), so None becomes the string "None". On timeout the group gets SIGTERM,
then SIGKILL after the grace period; if the group leader exits on SIGTERM first, grandchildren that ignore it are
SIGKILLed at once and get no grace period.
REQ-5: a job whose directory is in a Foremind project's state dir starts inside the project's shared admission
(upgrade.admitted), taken before the spec is written: Refused (an OSError, as an unwritable spec) when the project is
blocked. The runner gets that lock's descriptor (ADMISSION_FD) and takes it over before it writes its pid, so the lock
is held without a gap until the runner wrote the result; a runner started without one takes its own (the spec's
starting window covers the gap, upgrade checks again after its marker). The command itself does not inherit it.
A job elsewhere (no project) runs as before.
"""
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from foremind.fsutil import atomic_write

_PKG_PARENT = Path(__file__).resolve().parent.parent
_KILL_GRACE_S = 5
_START_S = 60
ADMISSION_FD = "FOREMIND_ADMISSION_FD"  # the runner's inherited admission lock (REQ-5); supervise's re-exec too


class Refused(OSError):
    """The project may not start a job now (upgrade.Blocked)."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def project_of(job_dir) -> Path | None:
    """The project whose state dir (`.foremind/`, paths.state_dir) holds `job_dir`; None outside every project."""
    p = Path(job_dir).resolve()
    return next((d.parent for d in (p, *p.parents) if d.name == ".foremind"), None)


def _admitted(root, fd=None):
    if root is None:
        return contextlib.nullcontext()
    from foremind import upgrade  # lazy: it imports this module
    return upgrade.admitted(root, fd)


def start(job_dir: Path, argv: list[str], *, cwd: Path, env: dict | None = None, timeout_s: int | None = None,
          job_id: str | None = None) -> str:
    """env overlays the inherited environment; only the overlay is written to spec.json. job_id: chosen by a caller
    that records it before the job exists (tick.start_job's intent); a new one otherwise."""
    from foremind.upgrade import Blocked
    job_id = job_id or uuid.uuid4().hex
    d = Path(job_dir).resolve() / job_id
    spec = d / "spec.json"
    env = {**{str(k): str(v) for k, v in (env or {}).items()}, "FOREMIND_JOB": job_id}
    try:
        with _admitted(project_of(d)) as fd:
            atomic_write(spec, json.dumps({"argv": list(argv), "cwd": str(Path(cwd).resolve()), "env": env,
                                           "timeout_s": timeout_s}, ensure_ascii=False))
            # cwd = package parent so `-m foremind` resolves to this code even from a source checkout
            try:
                p = subprocess.Popen([sys.executable, "-m", "foremind", "_job", "run", str(spec)],
                                     cwd=_PKG_PARENT, start_new_session=True, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     pass_fds=() if fd is None else (fd,),
                                     env=None if fd is None else {**os.environ, ADMISSION_FD: str(fd)})
            except BaseException:
                shutil.rmtree(d, ignore_errors=True)  # no runner: leave no half-made job behind
                raise
            # m2e.3: a thread holds and reaps the runner, so dropping p warns of no running subprocess and leaves no
            # zombie; _alive's waitpid may reap it first (the thread's wait then reads ChildProcessError as done)
            threading.Thread(target=p.wait, daemon=True).start()
            with contextlib.suppress(OSError):  # the job runs: the runner writes its pid as well (no pid -> lost)
                atomic_write(d / "pid", str(p.pid))
    except Blocked as e:
        raise Refused(f"{d}: not started: {e}") from None
    return job_id


def _kill_group(p):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(p.pid, signal.SIGTERM)
    try:
        code = p.wait(timeout=_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        code = None
    with contextlib.suppress(ProcessLookupError):  # the leader may be gone while children that ignore SIGTERM live on
        os.killpg(p.pid, signal.SIGKILL)
    return p.wait() if code is None else code


def run(spec_path) -> int:
    from foremind.upgrade import Blocked
    spec_path = Path(spec_path)
    d = spec_path.parent
    fd = os.environ.pop(ADMISSION_FD, None)  # not passed on to the command; closed when this process ends
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(_admitted(project_of(d), None if fd is None else int(fd)))
        except (Blocked, OSError, ValueError) as e:
            print(f"foremind _job: {d} not run: {e}", file=sys.stderr)
            return 1
        return _run(d, spec_path)


def _run(d, spec_path) -> int:
    atomic_write(d / "pid", str(os.getpid()))
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    started, timed_out = _now(), False
    with open(d / "stdout.log", "wb") as out, open(d / "stderr.log", "wb") as err:
        try:
            p = subprocess.Popen(spec["argv"], cwd=spec["cwd"], env={**os.environ, **spec["env"]},
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
        except OSError as e:
            err.write(f"foremind _job: cannot start: {e}\n".encode())
            code = 127
        else:
            try:
                code = p.wait(timeout=spec["timeout_s"])
            except subprocess.TimeoutExpired:
                timed_out = True
                code = _kill_group(p)
            else:  # M1-1-r1: leftovers in its group (background children) go with it
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(p.pid, signal.SIGTERM)
    atomic_write(d / "result.json", json.dumps({"exit_code": code, "started_at": started,
                                                "finished_at": _now(), "timed_out": timed_out}))
    return 0


def _alive(pid):
    with contextlib.suppress(ChildProcessError):
        if os.waitpid(pid, os.WNOHANG)[0]:  # our own child that already exited: reap it
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True  # ponytail: pid reuse reads as running; record process start time if that bites


def status(job_dir, job_id) -> dict:
    d = Path(job_dir) / job_id
    try:
        alive = _alive(int((d / "pid").read_text()))  # check before result.json so a job finishing in between is not "lost"
    except FileNotFoundError:
        age = time.time() - (d / "spec.json").stat().st_mtime  # no spec either: an unknown job, FileNotFoundError
        alive = None if age < _START_S else False  # start() has written the spec but not the pid (yet, or ever)
    try:
        return {"state": "done", **json.loads((d / "result.json").read_text())}
    except FileNotFoundError:
        return {"state": "starting" if alive is None else "running" if alive else "lost"}
