import sys

from foremind import carriers, config, lock, seat, worktree
from foremind.paths import ProjectNotFound, find_project_root


def register(sub):
    p = sub.add_parser("seat", help="open a seat for a ready batch (--user: I do it myself)")
    p.add_argument("batch_id")
    p.add_argument("--user", action="store_true", help="claim the batch for yourself and print its worktrees")
    p.set_defaults(func=_run)


def _run(args):
    try:
        root = find_project_root()
        if args.user:
            for rid, path in seat.claim_for_user(root, args.batch_id).items():
                print(f"{rid}\t{path}")
            return 0
        cfg = config.load(root, config.task_layer(seat.read_header(root, args.batch_id)))
        res = seat.open_seat(root, args.batch_id, carrier=carriers.get(seat.setting(cfg, "carrier.kind"), root, cfg))
    except (OSError, ProjectNotFound, ValueError, config.ConfigError, seat.SeatError, lock.LockError,
            worktree.WorktreeError, carriers.CarrierError, NotImplementedError) as e:
        print(f"foremind seat: {e}", file=sys.stderr)
        return 1
    if not res["ok"]:
        print(f"foremind seat: {res['batch']} not started (no start instruction sent):", file=sys.stderr)
        for p in res["problems"]:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"{res['session']}\t" + "\t".join(f"{rid}={p}" for rid, p in res["worktrees"].items()))
    return 0
