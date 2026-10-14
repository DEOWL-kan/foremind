"""`foremind` entry point. Subcommands live in foremind/commands/<name>.py, each with register(subparsers).
A module that fails to import or register is skipped with one stderr line: the rest, `hook` above all (its denies
keep the seats inside their boundary), must still run.

REQ-5: a command that may write runs inside the project's shared admission (upgrade.admitted), held until it returns,
so it never writes while `foremind upgrade` runs and is refused while an upgrade is unfinished or broken or the
project still needs one. What counts as reading is told by what the subcommand does with these arguments (_reads);
anything else, a command added later too, takes admission. Their own: `upgrade` (it takes the exclusive lock), `hook`
(hooks.main), `statusline` (a hot path that records telemetry only), `_job` (job.run, on the lock start passed) and
`supervise` (for its whole life, carried over its re-exec)."""
import argparse
import importlib
import pkgutil
import sys

from foremind import commands
from foremind.paths import ProjectNotFound, find_project_root

SKIPPED = "foremind: skipped command module"  # supervise's try run looks for it
OWN = ("upgrade", "hook", "statusline", "_job", "supervise")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="foremind")
    # metavar keeps internal commands (added without help=) out of --help
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    for m in pkgutil.iter_modules(commands.__path__):
        if not m.name.startswith("_"):
            try:
                importlib.import_module(f"{commands.__name__}.{m.name}").register(sub)
            except Exception as e:  # noqa: BLE001 — one broken command must not take the others down
                print(f"{SKIPPED} {m.name}: {type(e).__name__}: {e}", file=sys.stderr)
    args = parser.parse_args(argv)
    if args.command in OWN or _reads(args) or (root := _root(args)) is None:
        return args.func(args) or 0
    from foremind import upgrade  # here: a module that cannot be imported must not take `hook` down
    try:
        with upgrade.admitted(root):
            return args.func(args) or 0
    except upgrade.Blocked as e:
        print(f"foremind {args.command}: {e}", file=sys.stderr)
        return 1


def _reads(a) -> bool:
    """The forms that write nothing: diagnostics, kept while the project is blocked (REQ-5)."""
    c = a.command
    return (c in ("version", "status", "release-check", "watch")
            or c == "doctor" and not a.rescan
            or c == "quota" and not a.reset
            or c == "land" and a.notes
            or c == "decide" and not (a.new or a.notify or a.void or a.confirm or a.overturn or a.restore is not None)
            and (not a.args or a.args[0] == "show"))


def _root(args):
    """The project the command writes to; None (nothing to guard: the command reports the missing project itself)."""
    if args.command == "init":  # its root may be --root, a --repo, or a directory not set up yet
        from foremind import install
        from foremind.commands.init import _repos
        try:
            return _repos(args)[0]
        except (install.InstallError, OSError):
            return None  # init reports it, before writing anything
    try:
        return find_project_root()
    except ProjectNotFound:
        return None
