"""m2d.9: foremind/defaults.py, the one table of config defaults; catalog premises and plan validate read it.
m2e.2 (REQ-3): TABLE is a literal, the modules read it instead of their own copies, and each key is registered."""
import ast
import tempfile
import unittest
from pathlib import Path

from foremind import catalog, config, defaults, delivery, gate, hooks, repos
from foremind.plan import validate

PKG = Path(defaults.__file__).resolve().parent
# table() before m2e.2 (m2d.9, 61 keys): each value must read the same after the move
BEFORE = {
    "acceptance.timeout_min": 30, "audit.daily_cap": 10, "authz.preset": "balanced", "carrier.kind": "tmux",
    "context.abs_cap_tokens": 180000, "context.hard_pct": 80, "context.soft_pct": 65, "context.window_tokens": 200000,
    "decider.audit_ratio": 0.2, "delivery.depends_on": "merged", "delivery.level": "done",
    "delivery.repo.*.keep_updated": "never", "delivery.repo.*.merge_command": None,
    "delivery.repo.*.merge_method": None, "delivery.repo.*.push_pr": "user",
    "delivery.repo.*.target_branch": "<the repo's default branch>", "delivery.repo.*.update_method": "merge",
    "exclude.models": [], "exclude.providers": [], "gate.checks": [], "gate.ci": "none",
    "gate.ci_pending_max_min": 120, "hard_block.categories": [],
    "land.commands": ["python3 -m unittest discover -s tests"], "notify.channel": "none", "notify.p1": "push",
    "oneshot.timeout_min": 30, "plan.coupling.high": 0.6, "plan.coupling.history": 500, "plan.coupling.medium": 0.3,
    "plan.coupling.w_cochange": 0.3, "plan.coupling.w_ref": 0.4, "plan.coupling.w_semantic": 0.3,
    "quota.backoff_min": 15, "quota.low_pct": 85, "quota.oneshot_pause_pct": 95, "quota.pace": True,
    "quota.recover_pct": 80, "quota.reserve_pct": 10, "quota.stale_min": 15, "review.max_failures": 2,
    "review.max_rounds": 3, "seat.continue_enabled": False, "seat.manual_sessionstart_timeout_s": 600,
    "seat.permission_mode": "acceptEdits", "seat.sessionstart_timeout_s": 60, "seat.verify_timeout_s": 600,
    "stuck.api_retry_max": 3, "stuck.api_retry_min": 2, "stuck.ask_min": 20, "stuck.busy_tool_min": 120,
    "stuck.mark_min": 20, "stuck.remind_min": 20, "supervisor.gate_retry_min": 10,
    "supervisor.max_load_per_cpu": 0.8, "supervisor.max_oneshot": 2, "supervisor.max_seats": 2,
    "supervisor.merged_check_min": 5, "supervisor.min_free_mem_mb": 2048, "supervisor.seat_retries": 2,
    "supervisor.tick_s": 30}
ADDED = {  # REQ-3: keys read with a literal default somewhere, or registered with "unset" meaning none
    "stuck.pattern_repeat": 3, "oneshot.exclude_dynamic_prompt": False, "quota.probe_command": None,
    "review.max_budget_usd": None, "review.new_must_fix_max": None,
    **{f"review.cost_cap_{u}_{s}": None for u in ("usd", "tokens") for s in "sml"}}
SKIP = {"defaults.py", "config.py"}


def _literal(node) -> bool:
    try:
        ast.literal_eval(node)
    except ValueError:
        return False
    return True


def literal_defaults(path) -> list:
    """Calls in `path` where a TABLE key as a string literal is followed by a literal argument: a copy of a default."""
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            args = [*node.args, *(k.value for k in node.keywords)]
            out += [f"{path.name}:{a.lineno} {a.value}" for a, b in zip(args, args[1:])
                    if isinstance(a, ast.Constant) and a.value in defaults.TABLE and _literal(b)]
    return out


class DefaultsTest(unittest.TestCase):
    def test_the_table_is_what_the_code_read_before(self):
        self.assertEqual(defaults.TABLE, {**BEFORE, **ADDED})
        self.assertFalse(BEFORE.keys() & ADDED.keys())
        t = defaults.table()
        t["exclude.models"].append("x")
        t["gate.ci"] = "github"
        self.assertEqual(defaults.TABLE, {**BEFORE, **ADDED})  # a copy

    def test_defaults_imports_no_other_module(self):
        tree = ast.parse(Path(defaults.__file__).read_text(encoding="utf-8"))
        self.assertFalse([n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))])

    def test_no_module_keeps_a_copy(self):
        files = sorted(p for p in PKG.rglob("*.py") if p.relative_to(PKG).as_posix() not in SKIP)
        self.assertIn(PKG / "commands" / "review.py", files)
        hits = [f"{p.parent.relative_to(PKG)}/{h}" for p in files for h in literal_defaults(p)]
        self.assertEqual(hits, [])

    def test_the_scan_finds_a_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.py"
            p.write_text('cfg.get("acceptance.timeout_min", 30)\nf(cfg, "stuck.ask_min", default=[])\n'
                         'cfg.get("acceptance.timeout_min", TABLE["acceptance.timeout_min"])\n'
                         'EXAMPLES = {"gate.ci": "none"}\n', encoding="utf-8")
            self.assertEqual([h.split(" ")[1] for h in literal_defaults(p)],
                             ["acceptance.timeout_min", "stuck.ask_min"])

    def test_each_key_is_registered(self):
        self.assertEqual([k for k in defaults.TABLE if config._lookup(config.KEY_CLASSES, k) is None], [])

    def test_the_table_is_what_the_code_reads_unset(self):
        d = defaults.table()
        self.assertEqual(catalog.defaults(), d)
        self.assertEqual(d["context.abs_cap_tokens"], 180_000)  # the runtime's, not validate's old 150000
        self.assertEqual(hooks.budget({}, "seat", "m"), (130_000, 160_000))  # soft at 65/80 of it
        self.assertEqual(validate.effective_budget({}), 160_000)  # min(200000 x 80%, 180000): half 80000
        self.assertEqual(validate.effective_budget({"context.abs_cap_tokens": 100_000}), 100_000)
        self.assertEqual(d["delivery.repo.*.update_method"], delivery._method({}, "main"))
        self.assertIsNone(gate._merge_way({}, repos.Repo("main", Path("."))))  # no merge_method, no merge_command
        self.assertEqual((d["delivery.repo.*.merge_method"], d["delivery.repo.*.merge_command"]), (None, None))
        self.assertEqual(d["delivery.repo.*.keep_updated"], "never")

    def test_a_target_branch_premise_reads_the_repos_default_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "b").mkdir()
            cfg = {"repos": [{"id": "a", "path": ".", "default_branch": "trunk"}, {"id": "b", "path": "b"}]}
            holds = lambda k, v, c=cfg: catalog.holds(root, c, {"kind": "config_key", "key": k, "value": v})
            self.assertTrue(holds("delivery.repo.a.target_branch", "trunk"))
            self.assertFalse(holds("delivery.repo.a.target_branch", "main"))
            self.assertFalse(holds("delivery.repo.b.target_branch", None))  # none known
            self.assertFalse(holds("delivery.repo.a.target_branch", "trunk", {"repos": "bad"}))  # does not load
            self.assertTrue(holds("delivery.repo.a.update_method", "merge"))
            self.assertTrue(holds("delivery.repo.a.merge_method", None))

    def test_an_unset_none_key_premise_holds_for_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            holds = lambda k, v, c: catalog.holds(Path(tmp), c, {"kind": "config_key", "key": k, "value": v})
            for k in ("review.max_budget_usd", "review.cost_cap_tokens_m", "review.new_must_fix_max",
                      "quota.probe_command"):
                self.assertTrue(holds(k, None, {}), k)
                self.assertFalse(holds(k, 5, {}), k)
                self.assertTrue(holds(k, 5, {k: 5}), k)
            self.assertTrue(holds("oneshot.exclude_dynamic_prompt", False, {}))
            self.assertTrue(holds("stuck.pattern_repeat", 3, {}))


if __name__ == "__main__":
    unittest.main()
