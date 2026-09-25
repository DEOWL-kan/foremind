import sys

from foremind import hooks


def register(sub):
    p = sub.add_parser("hook")  # internal: run by Claude Code hook settings; no help= keeps it out of --help
    # free-form and optional: a missing or unknown event is a no-op, never an argparse exit 2 (= a block)
    p.add_argument("event", nargs="*")
    p.set_defaults(func=_run)


def _emit(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _run(args):
    hooks.main(args.event[0] if args.event else "", sys.stdin.buffer.read().decode("utf-8", "replace"), emit=_emit)
    return 0
