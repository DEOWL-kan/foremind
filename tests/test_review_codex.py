"""m2f REQ-13: Codex as the cross-vendor reviewer. A fake `codex` on PATH (as test_gate_fixture's fake claude) logs its
argv, writes the -o file and prints a --json event stream as tests set them; the real one is never run."""
import json
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from foremind import review, schemas
from foremind.paths import state_dir
from foremind.vendors import codex
from test_gate_fixture import APPROVED, TIERS, Project
from test_review import CHANGES, cli
from test_supervisor import Base

FAKE_CODEX = r'''
import json, os, sys
a = sys.argv[1:]
with open(os.environ["FAKE_CODEX_LOG"], "a") as f:
    f.write(json.dumps({"argv": a, "cwd": os.getcwd(), "role": os.environ.get("FOREMIND_ROLE")}) + "\n")
run = json.load(open(os.environ["FAKE_CODEX"]))
if run.get("message") is not None:
    open(a[a.index("-o") + 1], "w").write(run["message"])
sys.stdout.write(run.get("stdout", ""))
sys.exit(run.get("exit", 0))
'''
MODEL = "gpt-6-astra"
USAGE = {"input_tokens": 100, "cached_input_tokens": 40, "cache_write_input_tokens": 0, "output_tokens": 20,
         "reasoning_output_tokens": 7}
CROSS = {**TIERS, "review": "cross_vendor"}


def jsonl(*events):
    return "".join(json.dumps(e) + "\n" for e in events)


def turn(**usage):
    return {"type": "turn.completed", "usage": usage}


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def fake_codex(p):
    """Puts codex on the project's PATH and points the routes at it."""
    path = p.tmp / "bin" / "codex"
    path.write_text(f"#!{sys.executable}\n{FAKE_CODEX}")
    path.chmod(0o755)
    os.environ.update(FAKE_CODEX=str(p.tmp / "codex.json"), FAKE_CODEX_LOG=str(p.tmp / "codex.log"))
    codex_run(p)
    p.cfg["routes.reviewer.cross_model"] = f"codex:{MODEL}"


def codex_run(p, message=APPROVED, stdout=None, exit=0):
    """What the next fake codex run does: `message` (an object, raw text or None) into -o, `stdout` printed."""
    if stdout is None:
        stdout = jsonl({"type": "thread.started"}, turn(**USAGE))
    msg = message if message is None or isinstance(message, str) else json.dumps(message)
    (p.tmp / "codex.json").write_text(json.dumps({"message": msg, "stdout": stdout, "exit": exit}))


def codex_calls(p):
    f = p.tmp / "codex.log"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def project(case, tiers=CROSS):
    p = Project(case)
    fake_codex(p)
    p.batch(tiers=tiers)
    p.commit()
    return p


def exhausted(p, until, n=1):
    review.events(p.root).append("codex_quota_exhausted", batch="shop.1", n=n, until=iso(until))


QUOTA_OUT = jsonl({"type": "thread.started"}, {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}})


class RouteTest(unittest.TestCase):
    def who(self, p, h=None, now=None):
        return review.reviewer(p.cfg, h or review.load_batch(p.root, "shop.1"), p.root, now)

    def test_five_conditions_each_one_sends_it_back_to_claude(self):
        p = project(self)
        self.assertEqual(self.who(p), {"vendor": "codex", "model": MODEL, "effort": "xhigh",
                                       "relation": "cross_vendor"})
        h = review.load_batch(p.root, "shop.1")
        claude = {"vendor": "claude", "model": "claude-opus-5-5", "effort": "xhigh", "relation": "same_model"}
        self.assertEqual(self.who(p, {**h, "tiers": TIERS}), claude)  # tiers.review is not cross_vendor
        for change in ({"routes.reviewer.cross_model": None}, {"routes.reviewer.cross_model": MODEL},  # 1
                       {"routes.reviewer.cross_model": "codex:"},
                       {"exclude.providers": ["codex"]},  # 2
                       {"exclude.models": ["gpt-6-*"]}, {"exclude.models": ["codex:*"]}):  # 3
            with self.subTest(change):
                was = dict(p.cfg)
                p.cfg.update(change)
                self.assertEqual(self.who(p), claude)
                p.cfg = was
        self.assertEqual(self.who(p)["vendor"], "codex")
        with mock.patch.object(review.shutil, "which", return_value=None):  # 4
            self.assertEqual(self.who(p), claude)
        exhausted(p, time.time() + 600)  # 5
        self.assertEqual(self.who(p), claude)
        self.assertEqual(self.who(p, now=time.time() + 601)["vendor"], "codex")

    def test_relation_by_the_batch_model(self):
        p = project(self)
        h = review.load_batch(p.root, "shop.1")
        p.cfg["routes.reviewer.model"] = "claude-opus-4-8"
        self.assertEqual(self.who(p, {**h, "tiers": TIERS})["relation"], "same_vendor")
        self.assertEqual(self.who(p, {**h, "tiers": {**TIERS, "model": "claude-opus-4-8"}})["relation"], "same_model")
        self.assertEqual(self.who(p)["relation"], "cross_vendor")
        self.assertEqual(self.who(p, {**h, "tiers": {**CROSS, "model": f"codex:{MODEL}"}})["relation"], "same_model")
        # cross_vendor asked for but Claude reviews: never recorded as cross-vendor
        p.cfg["exclude.providers"] = ["codex"]
        self.assertEqual(self.who(p)["relation"], "same_vendor")

    def test_effort_goes_by_the_tiers_and_claude_exclusion_does_not_stop_codex(self):
        p = project(self)
        p.cfg.update({"routes.reviewer.effort_s": "low", "exclude.providers": ["claude"]})
        self.assertEqual((self.who(p)["vendor"], self.who(p)["effort"]), ("codex", "low"))
        p.cfg["routes.reviewer.effort_s"] = "huge"
        with self.assertRaisesRegex(review.FlowError, "reviewer effort"):
            self.who(p)


class VendorModuleTest(unittest.TestCase):
    def test_strict_schema(self):
        s = codex.strict_schema(review.REVIEW_SCHEMA)
        self.assertEqual((s["additionalProperties"], s["required"]), (False, ["verdict", "issues"]))
        self.assertEqual(s["properties"]["verdict"], review.REVIEW_SCHEMA["properties"]["verdict"])
        item = s["properties"]["issues"]["items"]
        self.assertEqual(item["required"], list(review.REVIEW_SCHEMA["properties"]["issues"]["items"]["properties"]))
        self.assertIs(item["additionalProperties"], False)
        self.assertEqual(item["properties"]["severity"], {"enum": list(schemas.SEVERITIES)})  # required: as it was
        self.assertEqual(item["properties"]["disputed"], {"anyOf": [{"type": "boolean"}, {"type": "null"}]})
        nested = codex.strict_schema({"type": "object", "properties": {"o": {"type": "object", "properties": {
            "x": {"type": "string"}}, "required": []}}})
        self.assertEqual(nested["properties"]["o"]["anyOf"][0], {"type": "object", "properties": {"x": {
            "anyOf": [{"type": "string"}, {"type": "null"}]}}, "required": ["x"], "additionalProperties": False})
        self.assertNotIn("additionalProperties", review.REVIEW_SCHEMA)  # the original is left alone

    def test_usage_is_the_last_turn(self):
        out = jsonl(turn(input_tokens=1), {"type": "item.completed"}) + "not json\n" + jsonl(turn(**USAGE))
        self.assertEqual(codex.usage(out), USAGE)
        self.assertEqual(codex.usage(jsonl(turn(input_tokens=3))),
                         {k: 3 if k == "input_tokens" else None for k in codex.USAGE_KEYS})
        self.assertIsNone(codex.usage(jsonl({"type": "thread.started"}) + "oops"))


class CodexReviewTest(unittest.TestCase):
    def test_launch_harvest_and_receipt(self):
        p = project(self)
        review.request(p.root, "shop.1", p.cfg)
        codex_run(p, {"verdict": "changes_requested", "issues": [
            {"severity": "must_fix", "location": "api:src/app.py:1", "summary": "bug", "conflict": False,
             "basis": "regression", "broken": "api:src/app.py:1", "disputed": None, "req": None, "quote": None,
             "form": None, "category": None},
            {"severity": "note", "location": "api:src/app.py:2", "summary": "nit", "conflict": False,
             "disputed": None, "basis": None, "req": None, "quote": None, "form": None, "broken": None,
             "category": None}]})
        session = review.start(p.root, "shop.1", p.cfg)
        started = p.events("review_started")[-1]
        self.assertEqual((started["vendor"], started["relation"], started["model"], started["unrestricted"]),
                         ("codex", "cross_vendor", MODEL, ["web__run", "image_gen", "collaboration.spawn_agent"]))
        r = p.harvest(session)
        mat = state_dir(p.root) / "reviews" / session
        argv = codex_calls(p)[-1]["argv"]
        self.assertEqual(argv, ["exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--sandbox",
                                "read-only", "--skip-git-repo-check", "-C", str(mat), "-m", MODEL,
                                "-c", 'model_reasoning_effort="xhigh"', "--output-schema", str(mat / "schema.json"),
                                "--json", "-o", str(mat / review.LAST_MESSAGE), review.PROMPT])
        for flag in ("--add-dir", "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust",
                     "--approve-for-me", "-p"):
            self.assertNotIn(flag, argv)
        self.assertEqual(codex_calls(p)[-1]["role"], "reviewer")
        self.assertEqual(json.loads((mat / "schema.json").read_text()), codex.strict_schema(review.REVIEW_SCHEMA))
        self.assertEqual(p.claude_calls(), [])
        # nulls dropped, then the same rules: the must_fix stands, the note gets its conflict
        self.assertEqual(schemas.validate("review_receipt", r), [])
        self.assertEqual((r["vendor"], r["relation"], r["model"], r["verdict"]),
                         ("codex", "cross_vendor", MODEL, "changes_requested"))
        self.assertEqual([(i["severity"], i.get("basis"), i.get("conflict")) for i in r["issues"]],
                         [("must_fix", "regression", None), ("note", None, "no")])
        self.assertEqual(p.state(), "changes_requested")
        ev = p.events("review_receipt")[-1]
        self.assertEqual({k: ev[k] for k in review.COST_KEYS}, dict.fromkeys(review.COST_KEYS))
        self.assertEqual((ev["codex_usage"], ev["vendor"], ev["relation"]), (USAGE, "codex", "cross_vendor"))
        self.assertEqual(review.spent_tokens(review.all_events(p.root), "shop.1"), (0, 1))  # uncounted, not zero
        # the status line readings are the Claude account's: none for a Codex review (r1)
        self.assertEqual((started["quota_start"], ev["quota_end"]), (None, None))

    def test_failures_without_a_run_record_codex_usage_too(self):  # r1
        p = project(self)
        p.cfg["review.max_failures"] = 3
        review.request(p.root, "shop.1", p.cfg)
        with mock.patch.object(review.job, "start", side_effect=OSError("no runner")):
            with self.assertRaisesRegex(review.FlowError, "did not start"):
                review.start(p.root, "shop.1", p.cfg)
        session = review.start(p.root, "shop.1", p.cfg)
        (state_dir(p.root) / "reviews" / session / "meta.json").unlink()  # start() cut short after in_review
        with self.assertRaisesRegex(review.FlowError, "never started"):
            review.harvest(p.root, "shop.1", session, p.cfg)
        for e in p.events("review_failed"):
            self.assertEqual({k: e[k] for k in (*review.COST_KEYS, "codex_usage", "quota_end")},
                             dict.fromkeys((*review.COST_KEYS, "codex_usage", "quota_end")))
        self.assertEqual(len(p.events("review_failed")), 2)

    def test_usage_null_when_unreported_and_no_message_is_counted(self):
        p = project(self)
        p.cfg["review.max_failures"] = 3
        review.request(p.root, "shop.1", p.cfg)
        codex_run(p, None, stdout="")
        with self.assertRaisesRegex(review.FlowError, "no last message"):
            p.harvest(review.start(p.root, "shop.1", p.cfg))
        e = p.events("review_failed")[-1]
        self.assertEqual((e["infra"], e["codex_usage"], e["cost_usd"]), (False, None, None))
        self.assertEqual(p.state(), "review_ready")
        self.assertEqual(p.events("codex_quota_exhausted"), [])

    def test_quota_backoff_doubles_resets_and_claude_reviews_meanwhile(self):
        p = project(self)
        review.request(p.root, "shop.1", p.cfg)
        codex_run(p, None, stdout=QUOTA_OUT, exit=1)
        t0 = time.time()
        with self.assertRaises(review.FlowError):
            p.harvest(review.start(p.root, "shop.1", p.cfg))
        e = p.events("review_failed")[-1]
        self.assertEqual((e["infra"], p.state()), (True, "review_ready"))  # not counted, back for another try
        q = p.events("codex_quota_exhausted")[-1]
        self.assertEqual((q["batch"], q["n"]), ("shop.1", 1))
        until = datetime.fromisoformat(q["until"]).timestamp()
        self.assertAlmostEqual(until, t0 + 15 * 60, delta=5)
        self.assertEqual(review.codex_backoff(review.all_events(p.root), t0), until)
        self.assertIsNone(review.codex_backoff(review.all_events(p.root), until))
        # within the back-off the cross_vendor batch goes to Claude, recorded as such
        r = p.harvest(review.start(p.root, "shop.1", p.cfg))
        self.assertEqual((r["vendor"], r["relation"], len(p.claude_calls()), len(codex_calls(p))),
                         ("claude", "same_model", 1, 1))
        self.assertEqual(p.events("review_receipt")[-1]["vendor"], "claude")  # no reset by a Claude review
        exhausted(p, t0 - 1)  # the back-off is over (its until passed); this one is the second in a row
        p.commit(text="x = 2\n")
        review.request(p.root, "shop.1", p.cfg)
        t1 = time.time()
        with self.assertRaises(review.FlowError):
            p.harvest(review.start(p.root, "shop.1", p.cfg))
        q = p.events("codex_quota_exhausted")[-1]
        self.assertEqual(q["n"], 3)
        self.assertAlmostEqual(datetime.fromisoformat(q["until"]).timestamp(), t1 + 60 * 60, delta=5)
        # a harvested Codex review starts the count over
        exhausted(p, t1 - 1, n=4)
        codex_run(p)
        r = p.harvest(review.start(p.root, "shop.1", p.cfg))
        self.assertEqual(r["vendor"], "codex")
        self.assertEqual(review._codex_exhausted(review.all_events(p.root)), [])
        p.commit(text="x = 3\n")
        review.request(p.root, "shop.1", p.cfg)
        codex_run(p, None, stdout=QUOTA_OUT, exit=1)
        with self.assertRaises(review.FlowError):
            p.harvest(review.start(p.root, "shop.1", p.cfg))
        self.assertEqual(p.events("codex_quota_exhausted")[-1]["n"], 1)

    def test_backoff_is_capped_at_five_hours(self):
        p = project(self)
        for _ in range(5):
            exhausted(p, time.time() - 1)
        review.request(p.root, "shop.1", p.cfg)
        codex_run(p, None, stdout=QUOTA_OUT, exit=1)
        t0 = time.time()
        with self.assertRaises(review.FlowError):
            p.harvest(review.start(p.root, "shop.1", p.cfg))
        q = p.events("codex_quota_exhausted")[-1]
        self.assertEqual(q["n"], 6)  # 15 × 2^5 = 480 minutes, capped
        self.assertAlmostEqual(datetime.fromisoformat(q["until"]).timestamp(), t0 + 5 * 3600, delta=5)

    def test_quota_words_in_what_codex_read_are_not_its_quota(self):
        p = project(self)
        p.cfg["review.max_failures"] = 3
        review.request(p.root, "shop.1", p.cfg)
        codex_run(p, "no json here", stdout=jsonl(
            {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "quota usage limit"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "rate limit"}}, turn(**USAGE)))
        with self.assertRaises(review.FlowError):
            p.harvest(review.start(p.root, "shop.1", p.cfg))
        self.assertEqual((p.events("review_failed")[-1]["infra"], p.events("codex_quota_exhausted")), (False, []))
        # an error item is the CLI's word
        codex_run(p, None, stdout=jsonl({"type": "item.completed", "item": {"type": "error", "message": "quota"}}),
                  exit=1)
        with self.assertRaises(review.FlowError):
            p.harvest(review.start(p.root, "shop.1", p.cfg))
        self.assertEqual((p.events("review_failed")[-1]["infra"], len(p.events("codex_quota_exhausted"))), (True, 1))

    def test_ab_refuses_a_codex_base_before_starting_anything(self):
        p = project(self)
        review.request(p.root, "shop.1", p.cfg)
        p.harvest(review.start(p.root, "shop.1", p.cfg))
        sessions = set((state_dir(p.root) / "reviews").iterdir())
        env = {k: v for k, v in os.environ.items() if k != "FOREMIND_SESSION"}
        (p.root / "foremind.toml").write_text(
            '[[repos]]\nid = "api"\npath = "api"\n\n[delivery.repo.api]\ntarget_branch = "main"\n')
        with mock.patch.dict(os.environ, {**env, "FOREMIND_ROLE": "controller"}, clear=True):
            code, _, err = cli(["review", "shop.1", "--ab", "low"])
        self.assertEqual(code, 1)
        self.assertIn("Codex", err)
        self.assertEqual((set((state_dir(p.root) / "reviews").iterdir()), len(codex_calls(p)), p.claude_calls(),
                          p.events("ab_review")), (sessions, 1, [], []))


class ClaudeUnchangedTest(unittest.TestCase):
    def test_argv_and_receipt_fields(self):
        p = Project(self)
        p.batch()
        head = p.commit()
        review.request(p.root, "shop.1", p.cfg)
        p.review_output(CHANGES)
        session = review.start(p.root, "shop.1", p.cfg)
        r = p.harvest(session)
        mat = state_dir(p.root) / "reviews" / session
        self.assertEqual(p.claude_calls()[-1]["argv"], [
            "-p", "--restricted", "--tools", "Read,Grep,Glob", "--permission-mode", "plan", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--model", "claude-opus-5-5", "--effort", "xhigh",
            "--output-format", "json", "--session-id", session, "--json-schema", json.dumps(review.REVIEW_SCHEMA),
            "--add-dir", str(mat / "api"), "--",
            "Read ./prompt.md and follow it. Output only the JSON object it asks for."])
        self.assertEqual({k: v for k, v in r.items() if k not in ("issues", "vendor", "relation")},
                         {"batch": "shop.1", "scope": "full", "heads": {"api": head}, "reviewer_session": session,
                          "model": "claude-opus-5-5", "effort": "xhigh", "round": 1, "verdict": "changes_requested"})
        self.assertEqual((r["vendor"], r["relation"]), ("claude", "same_model"))
        self.assertNotIn("unrestricted", p.events("review_started")[-1])
        ev = p.events("review_receipt")[-1]
        self.assertNotIn("codex_usage", ev)
        self.assertEqual((ev["vendor"], ev["relation"]), ("claude", "same_model"))


class StartReviewsTest(Base):
    def setUp(self):
        super().setUp()
        bin_ = self.tmp / "codex-bin"
        bin_.mkdir()
        (bin_ / "codex").write_text("#!/bin/sh\n")
        (bin_ / "codex").chmod(0o755)
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}"}))
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n'
                         f'[routes.reviewer]\ncross_model = "codex:{MODEL}"\n')
        self.started = []

        def start(root, bid, cfg, **kw):
            self.started.append(bid)
            review.set_state(root, bid, "in_review", expect=("review_ready",))
            return f"rev-{bid}"

        self.enterContext(mock.patch.object(review, "start", start))
        self.enterContext(mock.patch.object(review, "harvest", return_value=None))
        # p.1 Claude's (zero_context), p.2 cross_vendor
        self.plan("p", {"state": "review_ready"},
                  {"state": "review_ready", "tiers": {**TIERS, "review": "cross_vendor", "effort": "medium"}})

    def test_claude_out_of_quota_codex_still_starts(self):
        self.tick(five=99)
        q = json.loads((self.cfg_home / "quota.json").read_text())
        self.assertEqual(q["groups"]["claude/oneshot"]["state"], "exhausted")
        self.assertEqual(self.started, ["p.2"])

    def test_claude_low_halves_claudes_share_only(self):  # r1: max_oneshot 2, one Claude review takes the half
        self.tick(five=87)
        q = json.loads((self.cfg_home / "quota.json").read_text())
        self.assertEqual(q["groups"]["claude/oneshot"]["state"], "low")
        self.assertEqual(self.started, ["p.1", "p.2"])

    def test_codex_backing_off_claude_reviews_as_usual(self):
        self.log.append("codex_quota_exhausted", batch="p.2", n=1, until=iso(self.now + 600))
        self.tick()
        self.assertEqual(self.started, ["p.1", "p.2"])

    def test_both_on_claude_while_backing_off_and_out_of_quota(self):
        self.log.append("codex_quota_exhausted", batch="p.2", n=1, until=iso(self.now + 600))
        self.tick(five=99)
        self.assertEqual(self.started, [])


if __name__ == "__main__":
    unittest.main()
