"""Supervisor phases (m2b.1 item 6): each module here not starting with `_` is one more step of a tick, added without
touching tick.py. Modules are imported by name order; one that fails to import costs a `tick_error`, not the pass.

A module has:
  run(t, blocked)             required. Called once per pass on the Tick `t`, after repush and the full-block check,
                              before the quota probe; also under a full block, with blocked=True. While blocked is
                              True it must not start any model call (no job running a model, no review, no delivery)
  KIND                        optional: the job kind the module starts itself, through `t.start_job(KIND, key, argv,
                              timeout_s=…, **fields)` (intent `sv_<KIND>`; `batch` in fields when it is a batch's)
  after(t, it, ok, st, jid)   optional, with KIND: settles one ended job of that kind, as tick's own _after_<kind>:
                              `it` its intent, `ok` exit 0, `st` job.status, `jid` the job id; the tick writes the
                              result after it returns (raising leaves the job for the next pass)
Every call is wrapped: an exception costs a `tick_error` and the rest of the pass goes on. Wanting a model while quota
is unknown goes through `t.may_call(group)`, which also asks for the probe that runs after the phases.
"""
