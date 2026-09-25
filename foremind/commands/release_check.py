import sys

from foremind import config, gate, repos, review
from foremind.paths import ProjectNotFound, find_project_root


def register(sub):
    p = sub.add_parser("release-check", help="refuse a release range that contains unconfirmed provisional decisions")
    p.add_argument("range", help="git revision range to be released, e.g. v1.2..main")
    p.add_argument("--repo", help="repo id (required when the project has several)")
    p.set_defaults(func=_run)


def _run(args):
    try:
        root = find_project_root()
        known = repos.load_repos(root, config.load(root))
        pick = [r for r in known if r.id == args.repo] if args.repo else known
        if len(pick) != 1:
            raise review.FlowError(f"pass --repo <id> (repos: {[r.id for r in known]})")
        probs = gate.release_check(root, pick[0].path, args.range)
    except (review.FlowError, config.ConfigError, ProjectNotFound, OSError) as e:
        print(f"foremind release-check: {e}", file=sys.stderr)
        return 2
    for p in probs:
        print(f"FAIL {p}")
    print("release-check: " + ("blocked" if probs else "clean"))
    return 1 if probs else 0
