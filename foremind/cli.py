"""`foremind` entry point. Subcommands live in foremind/commands/<name>.py, each with register(subparsers).
A module that fails to import or register is skipped with one stderr line: the rest, `hook` above all (its denies
keep the seats inside their boundary), must still run."""
import argparse
import importlib
import pkgutil
import sys

from foremind import commands

SKIPPED = "foremind: skipped command module"  # supervise's try run looks for it


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
    return args.func(args) or 0
