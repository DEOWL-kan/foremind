"""`foremind upgrade [--check [--all] | --confirm-exit TOKEN]` (REQ-5): the user's offline project upgrade, on
foremind/upgrade.py (REQ-4). It migrates this project only: no pip or uv, no download, nothing stopped or resumed.

--check reads only (no state, event, lock file, backup or marker); with --all it checks every project in the user's
registry and this one, each once (resolved paths), and a registry entry that cannot be checked is a failure, never
skipped. Exit 0 when every check passes (an up-to-date project included), else 1. --confirm-exit records the user's
word for the unknown exit evidence the token names; it migrates nothing. Migrating and confirming refuse a Foremind
session, a one-shot role, the controller and a job (upgrade._identity); the guard denies `foremind upgrade` to a
session already, as any command outside its whitelist. No admission lock here: migrate takes the exclusive one."""
import sys
from pathlib import Path

from foremind import install, upgrade
from foremind.paths import ProjectNotFound, find_project_root, state_dir


def register(sub):
    p = sub.add_parser("upgrade", help="migrate this project to this Foremind (--check: only check)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="check only: write nothing")
    g.add_argument("--confirm-exit", metavar="TOKEN",
                   help="confirm that the work behind the unknown evidence --check printed has exited")
    p.add_argument("--all", action="store_true", help="with --check: every registered project and this one")
    p.set_defaults(func=_run)


def _fail(msg) -> int:
    print(f"foremind upgrade: {msg}", file=sys.stderr)
    return 1


def _roots(here) -> tuple[list[Path], list[str]]:
    """(projects to check, failures): the registry's entries and `here`, resolved and each once."""
    out, bad = [], []
    try:
        entries = install.registered()
    except (install.InstallError, OSError) as e:
        entries, bad = [], [f"{install.registry_path()}: cannot be read ({e}): fix it, then rerun"]
    for x in entries + ([str(here)] if here else []):
        p = Path(x).expanduser()
        if not p.is_absolute() or not state_dir(p).is_dir():
            bad.append(f"{install.registry_path()}: {x!r} is not the path of a Foremind project: fix or remove the "
                       "line, then rerun")
            continue
        if (p := p.resolve()) not in out:
            out.append(p)
    return out, bad


def _check(here, every) -> int:
    roots, bad = _roots(here) if every else ([here], [])
    ok = not bad
    for line in bad:
        print(f"refused: {line}")
    for r in roots:
        try:
            rep = upgrade.preflight(r)
        except (upgrade.UpgradeError, OSError, ValueError) as e:
            print(f"{r}: cannot be checked: {e}")
            ok = False
            continue
        print("\n".join(rep.lines()))
        ok = ok and rep.ok
    return 0 if ok else 1


def _run(args):
    if args.all and not args.check:
        print("foremind upgrade: --all goes with --check only", file=sys.stderr)
        return 2
    try:
        here = find_project_root()
    except ProjectNotFound as e:
        if not args.all:
            return _fail(e)
        here = None
    if args.check:
        return _check(here, args.all)
    try:
        if args.confirm_exit is not None:
            print("\n".join(upgrade.confirm_exit(here, args.confirm_exit)))
            return 0
        rep = upgrade.migrate(here)
    except (upgrade.UpgradeError, OSError) as e:
        return _fail(e)
    print("\n".join(rep.lines()))
    return 0 if rep.status in ("current", "migrated") else 1
