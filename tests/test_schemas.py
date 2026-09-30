import copy
import json
import unittest

from foremind.schemas import EXAMPLES, KINDS, SPECS, validate
from tests.test_templates import header

SHORT = "9fceb02"  # abbreviated SHA
UPPER = "9FCEB02D0AE598E95DC970B74767F19372D61AF8"


def _set(path, value):
    """Mutator: set obj[path] (path is a list of keys/indices); value=KeyError deletes."""
    def fn(obj):
        *head, last = path
        for k in head:
            obj = obj[k]
        if value is KeyError:
            del obj[last]
        else:
            obj[last] = value
    return fn


# kind -> [(mutator, substring expected in some error)]; every kind has >= 2 counterexamples
NEGATIVES = {
    "plan": [
        (_set(["goal_hash"], "e3b0c442"), "goal_hash"),
        (_set(["batches"], ["other.1"]), "batches[0]"),
        (_set(["revisions", 0, "n"], 2), "revisions[0].n"),
        (_set(["revisions", 0, "approved_by"], "decider"), "revisions[0].approved_by"),
        (_set(["plan_id"], KeyError), "plan_id: required"),
        (_set(["approved_by"], "controller"), "approved_by"),
        (_set(["approved_at"], "2026-01-05T09:30:00"), "approved_at: datetime needs a UTC offset"),
    ],
    "batch_header": [
        (_set(["owns_paths"], ["src/auth/token.py"]), "owns_paths[0]"),
        (_set(["owns_paths"], ["web:src/x.py"]), "owns_paths[0]: repo not listed"),
        (_set(["owns_paths"], ["api:../other/x.py"]), "owns_paths[0]"),
        (_set(["owns_paths"], ["api:/etc/passwd"]), "owns_paths[0]"),
        (_set(["mode"], "yolo"), "mode"),
        (_set(["accept_commands"], []), "accept_commands: length 0 < 1"),
        (_set(["id"], "auth"), "id"),
        (_set(["id"], "other.2"), "id: must start with plan_id"),
        (_set(["hard_block"], [24]), "hard_block[0]"),
        (_set(["tools"], [{"name": "t", "step": "s"}] * 6), "tools: length 6 > 5"),
        (_set(["tiers", "effort"], "ultra"), "tiers.effort"),
        (_set(["budget_estimate"], 80000), "budget_estimate: expected string"),
        (_set(["budget_estimate"], "0"), "budget_estimate"),
        (_set(["budget_estimate"], "8e4"), "budget_estimate"),
        (_set(["tools", 0, "step"], "  "), "tools[0].step: blank string"),
        (_set(["start_commands"], [5]), "start_commands[0]: expected string or object, got int"),
        (_set(["start_commands"], [{"repo": "api"}]), "start_commands[0].run: required"),
        (_set(["start_commands"], [{"run": "make", "cwd": "x"}]), "start_commands[0].cwd: unknown field"),
        (_set(["accept_commands"], [{"run": "make", "expect": "0"}]), "accept_commands[0].expect: expected integer"),
        (_set(["accept_commands"], [{"run": "make", "expect": 256}]), "accept_commands[0].expect"),
        (_set(["accept_commands"], [{"run": "make", "repo": "web"}]), "accept_commands[0].repo: repo not listed"),
    ],
    "handoff_section": [
        (_set(["state", "repos", "api", "sha"], SHORT), "state.repos.api.sha"),
        (_set(["decisions"], ["D1"]), "decisions[0]"),
        (_set(["failures"], ["auth.2.D1"]), "failures[0]"),
        (_set(["next"], []), "next: length 0 < 1"),
        (_set(["pointers"], KeyError), "pointers: required"),
        (_set(["goal"], " \n"), "goal: blank string"),
        (_set(["state", "last_test", "exit_code"], "1"), "state.last_test.exit_code"),
    ],
    "review_receipt": [
        (_set(["scope"], "partial"), "scope"),
        (_set(["batch"], "auth"), "batch"),
        (_set(["batch"], KeyError), "batch: required"),
        (_set(["heads", "api"], SHORT), "heads.api"),
        (_set(["heads", "api"], UPPER), "heads.api"),
        (_set(["heads"], {}), "heads: length 0 < 1"),
        (_set(["issues", 0, "severity"], "blocker"), "issues[0].severity"),
        (_set(["issues", 0, "status"], "old"), "issues[0].status"),
        (_set(["verdict"], "approved"), "verdict: must be 'changes_requested'"),
        (_set(["reviewer_session"], KeyError), "reviewer_session: required"),
        (_set(["round"], 0), "round: value 0 < 1"),
        (_set(["rebound_from"], {"api": SHORT}), "rebound_from.api"),
        (_set(["delta_from"], {"api": "a" * 40}), "delta_from: present exactly when scope is 'incremental'"),
        (_set(["scope"], "incremental"), "delta_from: present exactly when scope is 'incremental'"),
        (_set(["extra"], 1), "extra: unknown field"),
        (_set(["issues", 0, "filtered"], "no_basis"), "issues[0]: was and filtered go together"),  # REQ-15
        (_set(["issues", 0, "was"], "must_fix"), "issues[0]: was and filtered go together"),
        (_set(["issues", 0, "basis"], "taste"), "issues[0].basis"),
        (_set(["issues", 0, "req"], "REQ-0"), "issues[0].req"),
        (_set(["resolved"], ["A1B2C3D4E5F6"]), "resolved[0]"),  # REQ-16
        (_set(["unresolved"], ["a1b2c3d4e5f60718"] * 2), "unresolved: duplicate items"),
    ],
    "acceptance_result": [
        (_set(["heads", "app"], SHORT), "heads.app"),
        (_set(["commands"], []), "commands: length 0 < 1"),
        (_set(["commands", 0, "exit_code"], "0"), "commands[0].exit_code"),
        (_set(["commands", 0, "repo"], "api:src"), "commands[0].repo"),
        (_set(["commands", 1, "repo"], ""), "commands[1].repo"),
    ],
    "gate_result": [
        (_set(["heads", "api"], SHORT), "heads.api"),
        (_set(["checks", 0, "ok"], False), "verdict: must be 'fail'"),
        (_set(["verdict"], "maybe"), "verdict"),
    ],
    "pending": [
        (_set(["id"], "Q12"), "id"),
        (_set(["id"], "Q-0"), "id"),
        (_set(["id"], "PV-12"), "id"),
        (_set(["options"], ["a", "b", "c", "d", "e"]), "options: length 5 > 4"),
        (_set(["recommended"], 3), "recommended: option 3 out of range"),
        (_set(["answer"], 3), "answer: option 3 out of range"),
        (_set(["answer"], "1"), "answer: expected integer"),
        (_set(["category"], 24), "category: value 24 > 23"),
        (_set(["deadline"], "2026-01-07T09:30:00"), "deadline: datetime needs a UTC offset"),
        (_set(["deadline"], "next week"), "deadline"),
        (_set(["state"], "maybe"), "state"),
        *((_set(["state"], s), "answer: required in state") for s in
          ("awaiting_local_confirm", "answered", "applied", "provisional", "confirmed", "overturned", "overdue")),
    ],
    "decision_output": [
        (_set(["confidence"], "certain"), "confidence"),
        (_set(["question"], "12"), "question"),
        (_set(["conclusion"], 3), "conclusion: option 3 out of range"),
        (_set(["facts"], []), "facts: length 0 < 1"),
        (_set(["options"], ["only one"]), "options: length 1 < 2"),
        (_set(["options"], ["a", "b", "c", "d", "e"]), "options: length 5 > 4"),
        (_set(["precedents_cited"], ["J4"]), "precedents_cited[0]"),
        (_set(["precedents_cited"], KeyError), "precedents_cited: required"),
    ],
    "exemption": [
        (_set(["match"], {}), "match: needs at least one"),
        (_set(["match", "tools"], []), "match.tools: length 0 < 1"),
        (_set(["batch"], "auth"), "batch"),
        (_set(["expires_at"], "tomorrow"), "expires_at"),
        (_set(["category"], KeyError), "category: required"),
        (_set(["category"], 24), "category: value 24 > 23"),
    ],
    "provisional": [
        (_set(["id"], "P-3"), "id"),
        (_set(["id"], "PV-"), "id"),
        (_set(["question"], "PV-3"), "question"),
        (_set(["state"], "provisional"), "state: unknown field"),  # state lives on the Q-n record
        (_set(["revert"], KeyError), "revert: required"),
    ],
    "precedent": [
        (_set(["id"], "J4"), "id"),
        (_set(["question"], KeyError), "question: required"),
        (_set(["question"], "J-1"), "question"),
        (_set(["supersedes"], ["Q-1"]), "supersedes[0]"),
        (_set(["premises", 0, "kind"], "url"), "premises[0].kind"),
        (_set(["premises", 0, "sha256"], KeyError), "premises[0].sha256: required"),
        (_set(["premises", 2, "sha256"], "x"), "premises[2].sha256: unknown field"),
        (_set(["premises"], []), "premises: length 0 < 1"),
        (_set(["review_on"], "someday"), "review_on"),
        (_set(["review_on"], "20260401"), "review_on"),
        (_set(["review_on"], "2026-W14-3"), "review_on"),
    ],
    "audit_report": [
        (_set(["findings", 0, "evidence"], []), "findings[0].evidence: length 0 < 1"),
        (_set(["findings", 0, "evidence"], KeyError), "findings[0].evidence: required"),
        (_set(["findings", 0, "evidence"], [""]), "findings[0].evidence[0]"),
        (_set(["findings", 0, "severity"], "P4"), "findings[0].severity"),
        (_set(["trigger"], "whim"), "trigger"),
    ],
    "catalog_patch": [
        (_set([0, "op"], "rewrite"), "[0].op"),
        (_set([0, "file"], "roles/seat.md"), "[0].file"),
        (_set([0, "file"], "../config.toml"), "[0].file"),
        (_set([0, "file"], "precedents.md"), "[0].file"),
        (_set([0, "file"], "improvements.md"), "[0].file"),
        (_set([1, "content"], "x"), "[1].content: must be empty"),
        (_set([0, "content"], ""), "[0].content: required for append"),
        (_set([1, "op"], "replace"), "[1].content"),
        (_set([1, "anchor"], ""), "[1].anchor: required for delete"),
        (_set([1, "anchor"], KeyError), "[1].anchor: required for delete"),
    ],
    "catalog_output": [
        (_set(["patch", 0, "file"], "precedents.md"), "patch[0].file"),
        (_set(["patch"], [{"file": "rules.md", "op": "replace", "content": "y"}]), "patch[0].anchor: required"),
        (_set(["precedents", 0, "id"], "J4"), "precedents[0].id"),
        (_set(["improvements", 0, "evidence"], []), "improvements[0].evidence: length 0 < 1"),
        (_set(["improvements"], KeyError), "improvements: required"),
        (_set(["notes"], "x"), "notes: unknown field"),
    ],
    "controller_decision": [
        (_set(["reasons"], []), "reasons: length 0 < 1"),
        (_set(["scope_change", "add_owns_paths"], ["src/x.py"]), "scope_change.add_owns_paths[0]"),
        (_set(["plan_amend"], [{"batch": "auth.2", "changes": {}}]), "plan_amend[0].changes"),
        (_set(["merge"], True), "merge: unknown field"),
    ],
    "improvement_entry": [
        (_set(["category"], "oops"), "category"),
        (_set(["evidence"], []), "evidence: length 0 < 1"),
        (_set(["source"], ""), "source: length 0 < 1"),
    ],
    "delivery_notes": [
        (_set(["leftovers"], KeyError), "leftovers: required"),
        (_set(["config_keys", 0, "merge_class"], "global"), "config_keys[0].merge_class"),
        (_set(["config_keys", 0, "why"], ""), "config_keys[0].why"),
        (_set(["design"], [{"where": "§13.5"}]), "design[0].text: required"),
    ],
}


class SchemaTest(unittest.TestCase):
    def test_kinds_complete(self):
        required = {"plan", "batch_header", "handoff_section", "review_receipt", "acceptance_result",
                    "gate_result", "pending", "decision_output", "exemption", "provisional", "precedent",
                    "audit_report", "catalog_patch", "catalog_output", "controller_decision", "improvement_entry",
                    "delivery_notes"}
        self.assertEqual(set(KINDS), required)
        self.assertEqual(set(EXAMPLES), required)

    def test_unknown_kind_raises(self):
        with self.assertRaises(KeyError):
            validate("nope", {})

    def test_examples_valid(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(validate(kind, EXAMPLES[kind]), [])

    def test_counterexamples_rejected(self):
        for kind in KINDS:
            self.assertGreaterEqual(len(NEGATIVES[kind]), 2, kind)
            for mutate, expect in NEGATIVES[kind]:
                obj = copy.deepcopy(EXAMPLES[kind])
                mutate(obj)
                errs = validate(kind, obj)
                with self.subTest(kind=kind, expect=expect):
                    self.assertTrue(any(expect in e for e in errs), errs)

    def test_sha256_object_names_accepted(self):
        obj = copy.deepcopy(EXAMPLES["review_receipt"])
        obj["heads"]["api"] = "a" * 64
        self.assertEqual(validate("review_receipt", obj), [])
        obj["heads"]["api"] = "a" * 41
        self.assertTrue(validate("review_receipt", obj))

    def test_wrong_top_level_type(self):
        self.assertEqual(validate("plan", []), ["<root>: expected object, got list"])
        self.assertEqual(validate("catalog_patch", {}), ["<root>: expected array, got dict"])

    def test_batch_header_command_object_form(self):
        # §20 I45: a command is a string or {run, repo?, expect?}
        obj = copy.deepcopy(EXAMPLES["batch_header"])
        obj["start_commands"] = ["make", {"run": "make lint"}, {"run": "flutter test", "repo": "app", "expect": 1}]
        obj["accept_commands"] = [{"run": "pytest", "repo": "api"}]
        self.assertEqual(validate("batch_header", obj), [])

    def test_batch_header_allows_task_config_keys(self):
        obj = copy.deepcopy(EXAMPLES["batch_header"])
        obj["gate"] = {"rereview_after_update": "full"}
        self.assertEqual(validate("batch_header", obj), [])

    def test_optional_fields(self):
        receipt = copy.deepcopy(EXAMPLES["review_receipt"])
        receipt["rebound_from"] = {"api": "b" * 40}
        receipt["issues"] = []
        receipt["verdict"] = "approved"
        self.assertEqual(validate("review_receipt", receipt), [])
        receipt.update(scope="incremental", delta_from={"api": "c" * 40})  # REQ-6
        self.assertEqual(validate("review_receipt", receipt), [])
        # REQ-15/16: a lowered must_fix keeps was and filtered; the reconciliation with the previous receipt
        receipt.update(resolved=["a1b2c3d4e5f60718"], unresolved=[], issues=[{
            **EXAMPLES["review_receipt"]["issues"][0], "severity": "note", "was": "must_fix", "filtered": "no_basis",
            "basis": "req", "req": "REQ-1"}])
        self.assertEqual(validate("review_receipt", receipt), [])
        pending = copy.deepcopy(EXAMPLES["pending"])
        pending.update(state="awaiting_local_confirm", answer=1)
        self.assertEqual(validate("pending", pending), [])

    def test_catalog_patch_empty_list_ok(self):
        self.assertEqual(validate("catalog_patch", []), [])
        self.assertEqual(validate("catalog_output", {"patch": [], "precedents": [], "improvements": []}), [])

    def test_pending_answer_state_pairs(self):
        pending = copy.deepcopy(EXAMPLES["pending"])
        pending.update(state="answered", answer=1)
        self.assertEqual(validate("pending", pending), [])
        pending.update(state="open")
        del pending["answer"]
        self.assertEqual(validate("pending", pending), [])

    def test_catalog_patch_anchor_only_optional_for_append(self):
        for patch in ({"file": "capabilities.md", "op": "append", "content": "x"},
                      {"file": "index/api.md", "anchor": "## a", "op": "replace", "content": "y"},
                      {"file": "rules.md", "anchor": "- r", "op": "delete", "content": ""}):
            self.assertEqual(validate("catalog_patch", [patch]), [], patch)
        self.assertTrue(validate("catalog_patch", [{"file": "index.md", "op": "replace", "content": "y"}]))

    def test_batch_header_parses_from_real_header_text(self):
        # §20 I21: header scalars are strings; render a real header, parse it the way the template test does
        lines = [f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v}"
                 for k, v in EXAMPLES["batch_header"].items()]
        parsed = header("\n".join(["---", *lines, "---", ""]))
        self.assertEqual(parsed, EXAMPLES["batch_header"])
        self.assertEqual(validate("batch_header", parsed), [])
        self.assertIsInstance(parsed["budget_estimate"], str)

    def test_handoff_last_test_optional(self):
        obj = copy.deepcopy(EXAMPLES["handoff_section"])
        del obj["state"]["last_test"]
        self.assertEqual(validate("handoff_section", obj), [])

    def test_pending_answer_optional_and_provisional_has_no_state(self):
        pending = copy.deepcopy(EXAMPLES["pending"])
        pending["answer"] = 2
        self.assertEqual(validate("pending", pending), [])
        self.assertNotIn("state", EXAMPLES["provisional"])

    def test_decision_output_no_precedents_ok(self):
        obj = copy.deepcopy(EXAMPLES["decision_output"])
        obj["precedents_cited"] = []
        self.assertEqual(validate("decision_output", obj), [])

    def test_batch_header_fields_are_all_required(self):
        opt = ("contract", "coupling", "config_approved")
        for field in SPECS["batch_header"]["fields"]:
            if field in opt:
                continue
            obj = copy.deepcopy(EXAMPLES["batch_header"])
            del obj[field]
            with self.subTest(field=field):
                self.assertIn(f"{field}: required", validate("batch_header", obj))
        self.assertEqual([f for f, s in SPECS["batch_header"]["fields"].items() if s.get("opt")], list(opt))

    def test_batch_header_optional_fields(self):
        ok = {"contract": "false", "config_approved": "a" * 64,
              "coupling": [{"batch": "auth.1", "score": 0.5, "reason": "同改 token 格式"},
                           {"batch": "auth.3", "score": 1, "reason": "r"}]}
        self.assertEqual(validate("batch_header", {**EXAMPLES["batch_header"], **ok}), [])
        for field, bad, msg in [
            ("contract", "yes", "not one of"),
            ("contract", True, "expected string"),
            ("config_approved", "x" * 64, "does not match"),
            ("coupling", "高", "expected array"),
            ("coupling", [{"batch": "auth.1", "score": 1.5, "reason": "r"}], "value 1.5 > 1"),
            ("coupling", [{"batch": "auth.1", "score": -1, "reason": "r"}], "value -1 < 0"),
            ("coupling", [{"batch": "auth.1", "score": True, "reason": "r"}], "expected integer or number"),
            ("coupling", [{"batch": "auth.1", "score": "0.5", "reason": "r"}], "expected integer or number"),
            ("coupling", [{"batch": "auth", "score": 1, "reason": "r"}], "does not match"),
            ("coupling", [{"batch": "auth.1", "score": 1}], "reason: required"),
        ]:
            with self.subTest(field=field, bad=bad):
                errs = validate("batch_header", {**EXAMPLES["batch_header"], field: bad})
                self.assertTrue(errs and msg in errs[0], errs)


if __name__ == "__main__":
    unittest.main()
