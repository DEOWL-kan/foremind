"""`foremind` entry point. Subcommands live in foremind/commands/<name>.py, each with register(subparsers)."""
import argparse
import importlib
import pkgutil

from foremind import commands


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="foremind")
    # metavar keeps internal commands (added without help=) out of --help
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    for m in pkgutil.iter_modules(commands.__path__):
        if not m.name.startswith("_"):
            importlib.import_module(f"{commands.__name__}.{m.name}").register(sub)
    args = parser.parse_args(argv)
    return args.func(args) or 0
