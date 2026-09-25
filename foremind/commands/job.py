from foremind import job


def register(sub):
    p = sub.add_parser("_job")  # internal: no help= so it stays out of --help
    run = p.add_subparsers(dest="job_command", required=True).add_parser("run")
    run.add_argument("spec")
    run.set_defaults(func=lambda args: job.run(args.spec))
