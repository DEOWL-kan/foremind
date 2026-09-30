"""The default of each config key whose default is known (DESIGN §1.5; m2c.8, m2d.9, m2e.2 REQ-3): what the code reads
when no layer sets the key. The one source: the modules read TABLE, not copies of their own. config.py keeps none and
does not import this (a merged config holds only what is set: AUTHZ ceilings, L0 and precedent premises tell "unset"
apart); tests/test_defaults.py checks each key here is registered there.
None: unset means "not in force" (no cap, no command). A per-repo key is spelled as config.py spells it,
`delivery.repo.*.<key>`; a list key (UNION) unset is []. Not here: routes.*, context.by_role.*, and hooks' 1M-window
BIG_* (REQ-8)."""

DEFAULT_BRANCH = "<the repo's default branch>"  # delivery.repo.*.target_branch unset: review.target_branch's fallback

TABLE = {
    "acceptance.timeout_min": 30,
    "audit.daily_cap": 10,
    "authz.preset": "balanced",
    "carrier.kind": "tmux",
    # §6.3, the runtime's (hooks); a seat whose status line reports a 1M window gets hooks' REQ-8 ones instead
    "context.abs_cap_tokens": 180_000,
    "context.hard_pct": 80,
    "context.soft_pct": 65,
    "context.window_tokens": 200_000,
    "decider.audit_ratio": 0.2,
    "delivery.depends_on": "merged",
    "delivery.level": "done",
    "delivery.repo.*.keep_updated": "never",  # nothing reads it; only a merge_dev batch is updated (update.py)
    "delivery.repo.*.merge_command": None,  # gate._merge_way: then gh with merge_method, if any
    "delivery.repo.*.merge_method": None,  # gate._merge_way: no gh merge without one
    "delivery.repo.*.push_pr": "user",  # review.repo_cfg's (r1)
    "delivery.repo.*.target_branch": DEFAULT_BRANCH,  # review.target_branch
    "delivery.repo.*.update_method": "merge",  # delivery._method: unset (or "unknown") merges
    "exclude.models": [],
    "exclude.providers": [],
    "gate.checks": [],
    "gate.ci": "none",
    "gate.ci_pending_max_min": 120,
    "hard_block.categories": [],
    "land.commands": ["python3 -m unittest discover -s tests"],
    "notify.channel": "none",
    "notify.p1": "push",
    "oneshot.exclude_dynamic_prompt": False,
    "oneshot.timeout_min": 30,
    "plan.coupling.high": 0.6,
    "plan.coupling.history": 500,
    "plan.coupling.medium": 0.3,
    "plan.coupling.w_cochange": 0.3,
    "plan.coupling.w_ref": 0.4,
    "plan.coupling.w_semantic": 0.3,
    "quota.backoff_min": 15,
    "quota.low_pct": 85,
    "quota.oneshot_pause_pct": 95,
    "quota.pace": True,
    "quota.probe_command": None,
    "quota.recover_pct": 80,
    "quota.reserve_pct": 10,
    "quota.stale_min": 15,
    "review.cost_cap_tokens_l": None,
    "review.cost_cap_tokens_m": None,
    "review.cost_cap_tokens_s": None,
    "review.cost_cap_usd_l": None,
    "review.cost_cap_usd_m": None,
    "review.cost_cap_usd_s": None,
    "review.max_budget_usd": None,
    "review.max_failures": 2,
    "review.max_rounds": 3,
    "review.new_must_fix_max": None,
    "seat.continue_enabled": False,
    "seat.manual_sessionstart_timeout_s": 600,
    "seat.permission_mode": "acceptEdits",
    "seat.sessionstart_timeout_s": 60,
    "seat.verify_timeout_s": 600,
    "stuck.api_retry_max": 3,
    "stuck.api_retry_min": 2,
    "stuck.ask_min": 20,
    "stuck.busy_tool_min": 120,
    "stuck.mark_min": 20,
    "stuck.pattern_repeat": 3,
    "stuck.remind_min": 20,
    "supervisor.gate_retry_min": 10,
    "supervisor.max_load_per_cpu": 0.8,
    "supervisor.max_oneshot": 2,
    "supervisor.max_seats": 2,
    "supervisor.merged_check_min": 5,
    "supervisor.min_free_mem_mb": 2048,
    "supervisor.seat_retries": 2,
    "supervisor.tick_s": 30,
}


def table() -> dict:
    """A copy of TABLE (a caller may change it; lists are copied too)."""
    return {k: list(v) if isinstance(v, list) else v for k, v in TABLE.items()}
