import contextlib
import io
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from foremind import acceptance, review, schemas, seat, worktree
from foremind.cli import main
from foremind.fsutil import sha256_bytes
from foremind.paths import state_dir
from test_gate_fixture import Project, sh


def cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def issue(sev, loc, summary, **kw):
    return {"severity": sev, "location": loc, "summary": summary, **kw}


class RequestTest(unittest.TestCase):
    def test_needs_committed_heads(self):
        p = Project(self)
        p.batch()
        (p.wt() / "src").mkdir()
        (p.wt() / "src" / "app.py").write_text("x = 1\n")  # untracked counts as uncommitted
        with self.assertRaisesRegex(review.FlowError, "uncommitted"):
            review.request(p.root, "shop.1", p.cfg, session="fm-shop-shop.1-1")
        self.assertEqual(p.state(), "running")
        head = p.commit()
        out = review.request(p.root, "shop.1", p.cfg, session="fm-shop-shop.1-1")
        self.assertEqual((out["heads"], p.state()), ({"api": head}, "review_ready"))
        self.assertEqual(p.events("review_requested")[-1]["requested_by"], "fm-shop-shop.1-1")
        self.assertEqual(p.gh_log(), [])  # #23 unanswered = the user's: no push, no PR

    def test_pushes_and_opens_draft_pr_when_system(self):
        p = Project(self, origin=True)
        p.batch()
        head = p.commit()
        p.cfg.update({"delivery.repo.api.push_pr": "system", "gate.ci": "local_first"})
        out = review.request(p.root, "shop.1", p.cfg)
        self.assertEqual(sh(p.tmp, "git", "--git-dir", "api.git", "rev-parse", "refs/heads/fm/shop.1"), head)
        self.assertEqual(out["prs"], {"api": "https://example.test/pr/1"})
        create = [a for a in p.gh_log() if a[:2] == ["pr", "create"]]
        self.assertEqual(len(create), 1)
        self.assertIn("--draft", create[0])  # local_first: CI runs once, when the PR is marked ready
        self.assertEqual(create[0][create[0].index("--base") + 1], "main")
        review.request(p.root, "shop.1", p.cfg)  # same head again: PR reused
        self.assertEqual(len([a for a in p.gh_log() if a[:2] == ["pr", "create"]]), 1)
        self.assertEqual(p.state(), "review_ready")

    def test_push_never_overwrites_someone_elses_commit(self):
        p = Project(self, origin=True)
        p.batch()
        p.commit()
        p.cfg["delivery.repo.api.push_pr"] = "system"
        review.request(p.root, "shop.1", p.cfg)
        remote = lambda: sh(p.tmp, "git", "--git-dir", "api.git", "rev-parse", "refs/heads/fm/shop.1")  # noqa: E731
        sh(p.wt(), "git", "commit", "-q", "--amend", "-m", "reworded")  # the seat rewrites what we pushed: forced
        review.request(p.root, "shop.1", p.cfg)
        self.assertEqual(remote(), p.head())
        # someone else pushes onto the PR branch (another machine, the host's "Update branch" button) ...
        other = p.tmp / "other"
        sh(p.tmp, "git", "clone", "-q", "-b", "fm/shop.1", str(p.tmp / "api.git"), str(other))
        (other / "theirs.txt").write_text("theirs\n")
        sh(other, "git", "add", "-A")
        sh(other, "git", "commit", "-q", "-m", "theirs")
        sh(other, "git", "push", "-q", "origin", "fm/shop.1")
        theirs = remote()
        sh(p.wt(), "git", "fetch", "-q", "origin")  # ... and a background fetch refreshes refs/remotes/origin/
        p.commit(text="x = 3\n")
        with self.assertRaisesRegex(review.FlowError, "someone else"):
            review.request(p.root, "shop.1", p.cfg)
        self.assertEqual(remote(), theirs)
        sh(p.wt(), "git", "merge", "-q", "--no-edit", "origin/fm/shop.1")  # integrated: a plain push
        review.request(p.root, "shop.1", p.cfg)
        self.assertEqual(remote(), p.head())


    def test_push_is_recorded_before_the_request(self):
        p = Project(self, origin=True)
        p.batch()
        first = p.commit()
        p.cfg["delivery.repo.api.push_pr"] = "system"
        with mock.patch.object(review, "_pr_at", side_effect=review.FlowError("host down")):
            with self.assertRaises(review.FlowError):  # pushed, but no review_requested
                review.request(p.root, "shop.1", p.cfg)
        self.assertEqual(([e["head"] for e in p.events("review_push")], p.events("review_requested")), ([first], []))
        sh(p.wt(), "git", "commit", "-q", "--amend", "-m", "reworded")  # still known as our own push: forced
        review.request(p.root, "shop.1", p.cfg)
        self.assertEqual(sh(p.tmp, "git", "--git-dir", "api.git", "rev-parse", "refs/heads/fm/shop.1"), p.head())


class ReviewerTest(unittest.TestCase):
    def test_program_fields_overwrite_model_output(self):
        p = Project(self)
        p.batch()
        head = p.commit()
        review.request(p.root, "shop.1", p.cfg, session="fm-shop-shop.1-1")
        p.review_output({"verdict": "approved", "batch": "x.1", "heads": {"api": "0" * 40}, "round": 9,
                         "reviewer_session": "fm-shop-shop.1-1", "model": "sonnet", "rebound_from": {"api": "1" * 40},
                         "issues": [issue("note", "api:src/app.py:3", "Nit", id="99", fingerprint="f" * 16,
                                          status="repeat", extra=1)]})
        session = review.start(p.root, "shop.1", p.cfg)
        self.assertEqual(p.state(), "in_review")
        r = p.harvest(session)
        self.assertEqual(schemas.validate("review_receipt", r), [])
        self.assertEqual((r["batch"], r["heads"], r["reviewer_session"], r["round"], r["model"], r["effort"], r["scope"]),
                         ("shop.1", {"api": head}, session, 1, "claude-opus-5-5", "xhigh", "full"))
        self.assertNotIn("rebound_from", r)
        self.assertEqual(r["issues"], [{"id": "1", "fingerprint": review.fingerprint("api:src/app.py:3", "Nit"),
                                        "severity": "note", "status": "new", "location": "api:src/app.py:3",
                                        "summary": "Nit"}])
        path = state_dir(p.root) / "batches" / "shop.1.review.r1.json"
        self.assertEqual(p.events("review_receipt")[-1]["sha256"], sha256_bytes(path.read_bytes()))
        self.assertEqual(p.head(), head)  # the receipt lives in .foremind/, never in the worktree
        self.assertEqual(p.state(), "in_review")  # approval waits for acceptance and checks (the gate)
        self.assertEqual(review.harvest(p.root, "shop.1", session, p.cfg), r)  # harvesting twice is harmless

        call = p.claude_calls()[-1]
        argv = call["argv"]
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--mcp-config") + 1], '{"mcpServers":{}}')
        self.assertIn("--restricted", argv)  # §20 I47: no command tools, user/project settings ignored
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--session-id") + 1], session)
        self.assertEqual(p.events("review_started")[-1]["started_by"], "supervisor")
        mat = state_dir(p.root) / "reviews" / session
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(mat / "api"))  # §20 I50: not the live worktree
        self.assertNotIn("--bare", argv)
        self.assertEqual(call["role"], "reviewer")
        self.assertIn("./api.diff", (mat / "prompt.md").read_text())
        self.assertIn("./api/", (mat / "prompt.md").read_text())
        self.assertIn("+x = 1", (mat / "api.diff").read_text())

    def test_reviewer_reads_a_checkout_of_the_requested_heads(self):
        p = Project(self)
        p.batch()
        head = p.commit()
        review.request(p.root, "shop.1", p.cfg)
        (p.wt() / "src" / "app.py").write_text("uncommitted\n")  # the seat goes on editing
        (p.wt() / "notes.md").write_text("untracked\n")
        session = review.start(p.root, "shop.1", p.cfg)
        co = state_dir(p.root) / "reviews" / session / "api"
        self.assertEqual(((co / "src" / "app.py").read_text(), (co / "notes.md").exists()), ("x = 1\n", False))
        self.assertEqual(sh(co, "git", "rev-parse", "HEAD"), head)
        p.harvest(session)
        self.assertFalse(co.exists())  # removed and unregistered at harvest
        self.assertEqual(len(sh(p.wt(), "git", "worktree", "list").splitlines()), 2)

    def test_start_needs_the_requested_heads(self):
        p = Project(self)
        p.batch()
        p.commit()
        review.request(p.root, "shop.1", p.cfg)
        p.commit(text="x = 2\n")  # committed after `foremind review`: never pushed nor requested
        with self.assertRaisesRegex(review.FlowError, "run `foremind review` again"):
            review.start(p.root, "shop.1", p.cfg)
        self.assertEqual((p.state(), p.events("review_started"), p.claude_calls()), ("review_ready", [], []))
        self.assertEqual(len(sh(p.wt(), "git", "worktree", "list").splitlines()), 2)  # its checkout is gone
        self.assertEqual(p.run_review()["heads"], {"api": p.head()})

    def test_rounds_mark_repeat_disputed_and_new(self):
        p = Project(self)
        p.batch()
        p.commit()
        r1 = p.run_review({"verdict": "changes_requested",
                           "issues": [issue("must_fix", "api:src/app.py:3", "Uses local time zone.")]})
        self.assertEqual((r1["round"], r1["issues"][0]["status"], p.state()), (1, "new", "changes_requested"))
        p.commit(text="x = 2\n")
        r2 = p.run_review({"verdict": "changes_requested", "issues": [
            issue("must_fix", "api:src/app.py:7", "uses LOCAL time-zone"),  # moved line, other wording: same issue
            issue("should_fix", "api:src/app.py:9", "naming", disputed=True),
            issue("must_fix", "api:src/other.py:1", "missing test")]})
        self.assertEqual(r2["round"], 2)
        self.assertEqual([i["status"] for i in r2["issues"]], ["repeat", "disputed", "new"])
        self.assertNotIn("disputed", r2["issues"][1])
        self.assertIn(("changes_requested", "running"), [(e["prior"], e["state"]) for e in p.events("batch_state")])

    def test_failed_or_invalid_output_requeues(self):
        p = Project(self)
        p.batch()
        p.commit()
        p.cfg["review.max_failures"] = 3
        review.request(p.root, "shop.1", p.cfg)
        for bad in ("I cannot do that", {"verdict": "approved", "issues": [issue("must_fix", "api:a:1", "bug")]}):
            p.review_output(bad)
            session = review.start(p.root, "shop.1", p.cfg)
            with self.assertRaises(review.FlowError):
                p.harvest(session)
            self.assertEqual(p.state(), "review_ready")
        self.assertEqual(len(p.events("review_failed")), 2)
        self.assertEqual([e["round"] for e in p.events("review_started")], [1, 2])
        self.assertEqual(review.receipts(p.root, "shop.1"), [])

    def test_failures_on_the_same_heads_are_capped(self):
        p = Project(self)
        p.batch()
        head = p.commit()
        review.request(p.root, "shop.1", p.cfg)
        p.review_output("I cannot do that")
        for want in ("review_ready", "failed"):  # review.max_failures defaults to 2
            session = review.start(p.root, "shop.1", p.cfg)
            with self.assertRaises(review.FlowError):
                p.harvest(session)
            self.assertEqual(p.state(), want)
        last = p.events("batch_state")[-1]
        self.assertEqual((last["state"], last["reason"], last["failures"], last["heads"]),
                         ("failed", "review_failures", 2, {"api": head}))

    def test_out_of_quota_is_not_counted(self):
        p = Project(self)
        p.batch()
        p.commit()
        review.request(p.root, "shop.1", p.cfg)
        p.review_output("You've hit your session limit · resets 3pm")
        for _ in range(3):  # past review.max_failures 2
            session = review.start(p.root, "shop.1", p.cfg)
            with self.assertRaises(review.FlowError):
                p.harvest(session)
            self.assertEqual(p.state(), "review_ready")
        self.assertEqual([e["infra"] for e in p.events("review_failed")], [True, True, True])

    def test_worktrees_at_the_seat_path(self):
        p = Project(self)
        p.cfg["project.name"] = "my shop"
        p.batch()
        want = worktree.batch_dir(seat.project_slug(p.root, p.cfg), "shop.1") / "api"
        self.assertTrue(want.is_dir())
        self.assertEqual([wt for _, wt in review.batch_repos(p.root, review.load_batch(p.root, "shop.1"), p.cfg)],
                         [want])
        self.assertFalse((Path(os.environ["FOREMIND_WT_ROOT"]) / "shop").exists())  # not the directory name

    def test_reviewer_that_never_started_gives_the_batch_back(self):
        p = Project(self)
        p.batch()
        p.commit()  # review.max_failures 2, but machine failures are not counted
        review.request(p.root, "shop.1", p.cfg)
        with mock.patch.object(review.job, "start", side_effect=OSError("no runner")):
            with self.assertRaisesRegex(review.FlowError, "did not start"):
                review.start(p.root, "shop.1", p.cfg)
        self.assertEqual((p.state(), len(p.events("review_failed"))), ("review_ready", 1))
        with mock.patch.object(review.job, "start", side_effect=KeyboardInterrupt):  # crash before meta.json
            with self.assertRaises(KeyboardInterrupt):
                review.start(p.root, "shop.1", p.cfg)
        self.assertEqual(p.state(), "in_review")
        session = p.events("review_started")[-1]["reviewer_session"]
        with self.assertRaisesRegex(review.FlowError, "never started"):
            review.harvest(p.root, "shop.1", session, p.cfg)
        self.assertEqual(p.state(), "review_ready")  # judged by the review_started heads
        session = review.start(p.root, "shop.1", p.cfg)
        jobs = state_dir(p.root) / "jobs"
        jid = json.loads((state_dir(p.root) / "reviews" / session / "meta.json").read_text())["job_id"]
        acceptance.wait(jobs, jid)
        shutil.rmtree(jobs / jid)  # the runner's files are lost: OSError, not ValueError
        with self.assertRaisesRegex(review.FlowError, "lost"):
            review.harvest(p.root, "shop.1", session, p.cfg)
        self.assertEqual(p.state(), "review_ready")
        self.assertEqual([e["infra"] for e in p.events("review_failed")], [True, True, True])
        self.assertEqual(p.run_review()["verdict"], "approved")

    def test_crash_between_receipt_event_and_file(self):
        p = Project(self)
        p.batch()
        p.commit()
        review.request(p.root, "shop.1", p.cfg)
        p.review_output({"verdict": "changes_requested", "issues": [issue("must_fix", "api:src/app.py:1", "bug")]})
        session = review.start(p.root, "shop.1", p.cfg)
        real = review.atomic_write

        def crash(path, data):
            if ".review.r" in Path(path).name:
                raise KeyboardInterrupt
            return real(path, data)

        with mock.patch.object(review, "atomic_write", crash), self.assertRaises(KeyboardInterrupt):
            p.harvest(session)
        self.assertEqual((len(p.events("review_receipt")), review.receipts(p.root, "shop.1")), (1, []))  # event first
        r = p.harvest(session)
        path = review.receipts(p.root, "shop.1")[-1][1]
        self.assertEqual(p.events("review_receipt")[-1]["sha256"], sha256_bytes(path.read_bytes()))
        self.assertEqual((r["verdict"], p.state()), ("changes_requested", "changes_requested"))

    def test_excluded_model_is_never_started(self):
        p = Project(self)
        p.batch()
        p.commit()
        review.request(p.root, "shop.1", p.cfg)
        p.cfg["exclude.models"] = ["claude:*OPUS*"]
        with self.assertRaisesRegex(review.FlowError, "excluded"):
            review.start(p.root, "shop.1", p.cfg)
        self.assertEqual((p.state(), p.claude_calls()), ("review_ready", []))

    def test_output_parsing(self):
        self.assertEqual(review.parse_output('Here:\n```json\n{"verdict": "approved", "issues": []}\n```'),
                         {"verdict": "approved", "issues": []})
        wrapped = json.dumps({"type": "result", "is_error": True, "result": "rate limited"})
        with self.assertRaisesRegex(ValueError, "error"):
            review.parse_output(wrapped)
        prose = 'Checked {a, b} first. {"verdict": "changes_requested", "issues": []} Done; see {x}.'
        self.assertEqual(review.parse_output(prose)["verdict"], "changes_requested")


class DiagnoseTest(unittest.TestCase):
    def rec(self, *statuses, scope="full", verdict="changes_requested"):
        return {"scope": scope, "verdict": verdict, "issues": [{"status": s} for s in statuses]}

    def test_cap_and_trend(self):
        r = self.rec
        self.assertEqual(review.diagnose([r("new"), r("repeat")]), [])  # under the cap
        self.assertEqual(review.diagnose([r("new"), r("repeat"), r("repeat", "new")]), ["escalate_fixer"])
        self.assertEqual(review.diagnose([r("new"), r("new"), r("new", "new", "repeat")]), ["freeze_scope_or_split"])
        self.assertEqual(review.diagnose([r("new"), r("new"), r("disputed")]), ["arbitrate"])
        self.assertEqual(review.diagnose([r("new"), r("new", scope="delta"), r("repeat")]), [])  # delta rounds don't count
        self.assertEqual(review.diagnose([r("new"), r("new"), r(verdict="approved")]), [])
        self.assertEqual(review.diagnose([r("new"), r("repeat")], max_rounds=2), ["escalate_fixer"])


class CliTest(unittest.TestCase):
    def test_review_and_run_reviewer(self):
        p = Project(self)
        p.batch()
        p.commit()
        (p.root / "foremind.toml").write_text(
            '[[repos]]\nid = "api"\npath = "api"\n\n[delivery.repo.api]\ntarget_branch = "main"\n')
        code, out, err = cli(["review", "shop.1"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["state"], "review_ready")
        with mock.patch.dict(os.environ, {"FOREMIND_SESSION": "fm-shop-shop.1-1", "FOREMIND_ROLE": "seat"}):
            code, _, err = cli(["review", "shop.1", "--run-reviewer"])  # a seat cannot run its own reviewer
        self.assertEqual((code, p.claude_calls(), p.state()), (1, [], "review_ready"))
        self.assertIn("supervisor", err)
        code, out, _ = cli(["review", "shop.1", "--run-reviewer"])
        self.assertEqual((code, json.loads(out)["verdict"]), (0, "approved"))
        self.assertEqual(p.events("review_started")[-1]["started_by"], "user")
        code, _, err = cli(["review", "nope"])
        self.assertEqual(code, 1)
        self.assertIn("bad batch id", err)
        with (state_dir(p.root) / "events.jsonl").open("a") as f:
            f.write("{torn line\n")
        code, _, err = cli(["review", "shop.1"])
        self.assertEqual((code, len(err.strip().splitlines())), (1, 1))
        self.assertIn("corrupt JSON", err)


class RunTest(unittest.TestCase):
    def test_timeout_is_a_flow_error_and_the_module_default_applies(self):  # M1-7 SF-4
        with self.assertRaisesRegex(review.FlowError, "no answer within 0.2 s"):
            review.run(["sleep", "5"], ".", timeout=0.2)
        with mock.patch.object(review, "TIMEOUT_S", 0.2), self.assertRaises(review.FlowError):
            review.git(".", "-c", "alias.nap=!sleep 5", "nap")
        self.assertEqual(review.run(["true"], ".").returncode, 0, "no bound by default")


if __name__ == "__main__":
    unittest.main()
