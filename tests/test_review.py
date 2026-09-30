import contextlib
import io
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from foremind import acceptance, batchlog, delivery, gate, inbox, lock, oneshot, review, schemas, seat, worktree
from foremind import header as hdr
from foremind.cli import main
from foremind.fsutil import sha256_bytes
from foremind.paths import state_dir
from test_gate_fixture import APPROVED, Project, sh


def cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def issue(sev, loc, summary, **kw):
    return {"severity": sev, "location": loc, "summary": summary, **kw}


REG = {"basis": "regression", "broken": "api:src/app.py:1"}  # REQ-15: a must_fix with a basis stands


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


    def test_same_heads_with_a_receipt_are_not_reviewed_again(self):  # N9
        p = Project(self)
        p.batch()
        p.commit()
        p.run_review(CHANGES)
        n = len(p.events("review_requested"))
        with self.assertRaisesRegex(review.FlowError, r"already have review receipt r1 \(changes_requested\).*"
                                                      r"--request-changes"):
            review.request(p.root, "shop.1", p.cfg, session="fm-shop-shop.1-1")  # another reviewer, same code
        self.assertEqual((p.state(), len(p.events("review_requested"))), ("changes_requested", n))
        for args in (("--allow-empty", "-m", "empty"), ("--amend", "--allow-empty", "-m", "reworded")):  # r3 #2: same trees
            sh(p.wt(), "git", "commit", "-q", *args)
            with self.assertRaisesRegex(review.FlowError, "already have review receipt r1"):
                review.request(p.root, "shop.1", p.cfg)
        p.commit(text="x = 2\n")
        self.assertEqual(review.request(p.root, "shop.1", p.cfg)["state"], "review_ready")

    def test_a_written_rebuttal_does_not_reopen_the_same_heads(self):  # REQ-19, controller m2b.7 r3
        p = Project(self)
        p.batch()
        p.commit()
        p.run_review(CHANGES)
        batchlog.append(p.root, "shop.1", "## 书面反驳\n\nr1 #1：x = 1 是约定值（D3）。", author="fm-shop-shop.1-1")
        with self.assertRaisesRegex(review.FlowError, "already have review receipt r1"):
            review.request(p.root, "shop.1", p.cfg, session="fm-shop-shop.1-1")
        self.assertEqual(p.state(), "changes_requested")

    def test_asking_again_keeps_the_base_of_a_repo_merged_with_a_merge_commit(self):  # r1 #2
        p = Project(self, ("api", "app"))
        p.cfg["gate.checks"] = ["true"]
        p.batch()
        for r in ("api", "app"):
            p.commit(r)
        p.run_review()
        self.assertEqual(gate.run(p.root, "shop.1", p.cfg)["state"], "delivered")
        base = p.events("review_requested")[-1]["bases"]["app"]
        sh(p.root / "app", "git", "merge", "-q", "--no-ff", "-m", "user merges app", "fm/shop.1")
        p.commit("api", text="x = 2\n")  # the seat goes on in api: a new round
        review.request(p.root, "shop.1", p.cfg)
        self.assertEqual(p.events("review_requested")[-1]["bases"]["app"], base)
        pairs = review.batch_repos(p.root, review.load_batch(p.root, "shop.1"), p.cfg)
        hd = review.heads(pairs)
        app, wt = pairs[1]
        self.assertTrue(gate.merged(app, wt, hd["app"], p.cfg, review.bases(p.root, "shop.1", pairs, hd)["app"]))

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
        goal = state_dir(p.root) / "plans" / "shop" / "goal.md"
        goal.parent.mkdir(parents=True)
        goal.write_text("REQ-1: 冻结目标\n")
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
        self.assertIn("./goal.md", (mat / "prompt.md").read_text())  # the frozen goal (written before start)
        self.assertEqual((mat / "goal.md").read_text(), "REQ-1: 冻结目标\n")
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
                           "issues": [issue("must_fix", "api:src/app.py:3", "Uses local time zone.", **REG)]})
        self.assertEqual((r1["round"], r1["issues"][0]["status"], p.state()), (1, "new", "changes_requested"))
        p.commit(text="x = 2\n")
        r2 = p.run_review({"verdict": "changes_requested", "issues": [
            issue("must_fix", "api:src/app.py:7", "uses LOCAL time-zone", **REG),  # moved line, other wording: same issue
            issue("should_fix", "api:src/app.py:9", "naming", disputed=True),
            issue("must_fix", "api:src/other.py:1", "missing test", **REG)]})
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
        self.assertEqual(review.load_batch(p.root, "shop.1")["state_prior"], "in_review")  # §20 I1, for L0 reconcile

    def test_timed_out_reviewer_is_the_machines_failure(self):  # §10.5: interrupted, discarded and rerun
        """Not counted against review.max_failures; counted on their own (r1 #4, r2 #2, r3), so a review that always
        outgrows oneshot.timeout_min stops at review.MAX_TIMEOUTS consecutive timeouts on the same heads."""
        p = Project(self)
        p.batch()
        head = p.commit()
        review.request(p.root, "shop.1", p.cfg)
        states = []
        for timed_out in (True, False, True, True, True):  # max_failures 2; the model failure breaks the streak
            session = review.start(p.root, "shop.1", p.cfg)
            jid = json.loads((state_dir(p.root) / "reviews" / session / "meta.json").read_text())["job_id"]
            acceptance.wait(state_dir(p.root) / "jobs", jid)
            st = {"state": "done", "exit_code": -9, "timed_out": True} if timed_out else {"state": "done",
                                                                                         "exit_code": 1}
            with mock.patch.object(review.job, "status", return_value=st):
                with self.assertRaisesRegex(review.FlowError, "timed out" if timed_out else "exit_code"):
                    p.harvest(session)
            states.append(p.state())
        self.assertEqual(states, ["review_ready"] * 4 + ["failed"])
        self.assertEqual([(e["infra"], e["timed_out"]) for e in p.events("review_failed")],
                         [(True, True), (False, False), (True, True), (True, True), (True, True)])
        last = p.events("batch_state")[-1]
        self.assertEqual((last["reason"], last["failures"], last["heads"]), ("review_timeouts", 3, {"api": head}))
        review.events(p.root).append("run_requested", batches=["shop.1"])  # `foremind run shop.1`: from zero
        review.set_state(p.root, "shop.1", "ready")
        review.set_state(p.root, "shop.1", "running")
        review.request(p.root, "shop.1", p.cfg)
        session = review.start(p.root, "shop.1", p.cfg)
        jid = json.loads((state_dir(p.root) / "reviews" / session / "meta.json").read_text())["job_id"]
        acceptance.wait(state_dir(p.root) / "jobs", jid)
        with mock.patch.object(review.job, "status", return_value={"state": "done", "exit_code": -9, "timed_out": True}):
            with self.assertRaisesRegex(review.FlowError, "timed out"):
                p.harvest(session)
        self.assertEqual(p.state(), "review_ready")

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
        p.review_output(CHANGES)
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


CHANGES = {"verdict": "changes_requested", "issues": [issue("must_fix", "api:src/a.py:1", "bug", **REG)]}


class IncrementalTest(unittest.TestCase):  # REQ-6
    def mat(self, p):
        return state_dir(p.root) / "reviews" / p.events("review_started")[-1]["reviewer_session"]

    def target_moves(self, p, text="target\n"):
        d = p.root / "api"
        (d / "t.txt").write_text(text)
        sh(d, "git", "add", "-A")
        sh(d, "git", "commit", "-q", "-m", "target moves")

    def test_second_round_reviews_the_increment(self):
        p = Project(self)
        p.cfg["gate.checks"] = ["true"]
        p.batch()
        p.commit(path="src/a.py", text="a = 1\n")
        batchlog.append(p.root, "shop.1", "- 开工记录", author="fm-shop-shop.1-1")
        r1 = p.run_review(CHANGES)
        self.assertEqual((r1["scope"], "delta_from" in r1), ("full", False))
        self.assertTrue((self.mat(p) / "api.diff").is_file())
        batchlog.append(p.root, "shop.1", "- 答复：已改", author="fm-shop-shop.1-1")
        head = p.commit(path="src/b.py", text="b = 2\n")
        r2 = p.run_review()
        self.assertEqual(schemas.validate("review_receipt", r2), [])
        self.assertEqual((r2["scope"], r2["delta_from"], r2["heads"]), ("incremental", r1["heads"], {"api": head}))
        mat = self.mat(p)
        delta = (mat / "api.delta.diff").read_text()
        self.assertIn("+b = 2", delta)
        self.assertNotIn("a = 1", delta)
        self.assertEqual((mat / "api.files.txt").read_text().split(), ["src/a.py", "src/b.py"])
        since = (mat / "log-since.md").read_text()
        self.assertIn("答复：已改", since)
        self.assertNotIn("开工记录", since)
        self.assertEqual([(mat / f).exists() for f in ("api.diff", "log.md")], [False, False])
        self.assertEqual(json.loads((mat / "previous-receipt.json").read_text())["round"], 1)
        prompt = (mat / "prompt.md").read_text()
        self.assertTrue(prompt.startswith(f"本轮是增量重审，前一轮 heads 为 {json.dumps(r1['heads'])}"))
        self.assertIn("## 增量轮", prompt)
        meta = json.loads((mat / "meta.json").read_text())
        self.assertEqual((meta["scope"], meta["delta_from"], meta["difficulty"], meta["security"]),
                         ("incremental", r1["heads"], "S", True))  # no goal.md: taken as guarded
        self.assertEqual(gate.run(p.root, "shop.1", p.cfg)["verdict"], "pass")

    def test_full_again_after_a_target_merge_or_a_rewrite(self):
        p = Project(self)
        p.batch()
        p.commit()
        p.run_review(CHANGES)
        self.target_moves(p)
        sh(p.wt(), "git", "merge", "-q", "--no-edit", "main")  # the merge-base changes
        p.commit(text="x = 2\n")
        self.assertEqual(p.run_review(CHANGES)["scope"], "full")
        self.assertTrue((self.mat(p) / "api.diff").is_file())
        (p.wt() / "src" / "app.py").write_text("x = 3\n")
        sh(p.wt(), "git", "commit", "-q", "-a", "--amend", "-m", "rewritten")  # r2's heads are no ancestor
        r3 = p.run_review()
        self.assertEqual((r3["scope"], "delta_from" in r3), ("full", False))

    def test_after_a_rebound_the_increment_starts_at_its_heads(self):
        p = Project(self)
        p.cfg["gate.checks"] = ["true"]
        p.batch()
        p.commit()
        p.run_review()
        self.assertEqual(gate.run(p.root, "shop.1", p.cfg)["verdict"], "pass")
        self.target_moves(p)
        self.assertEqual(delivery.update(p.root, "shop.1", p.cfg, by="user")["outcome"], "rebound")
        rebound = review.load_receipts(p.root, "shop.1")[-1]
        p.commit(path="src/b.py", text="b = 2\n")
        r = p.run_review()
        self.assertEqual((r["scope"], r["delta_from"]), ("incremental", rebound["heads"]))
        delta = (self.mat(p) / "api.delta.diff").read_text()
        self.assertIn("+b = 2", delta)
        self.assertNotIn("t.txt", delta)  # the update's own merge is not in the increment
        self.assertEqual(gate.run(p.root, "shop.1", p.cfg)["verdict"], "pass")

    def test_a_rebound_incremental_receipt_still_passes_the_gate(self):  # r1 #1
        p = Project(self)
        p.cfg["gate.checks"] = ["true"]
        p.batch()
        p.commit()
        p.run_review(CHANGES)
        p.commit(text="x = 2\n")
        r2 = p.run_review()
        self.assertEqual(gate.run(p.root, "shop.1", p.cfg)["verdict"], "pass")
        for text in ("target\n", "target 2\n"):  # rebound twice: both copies keep r2's scope and delta_from
            self.target_moves(p, text)
            self.assertEqual(delivery.update(p.root, "shop.1", p.cfg, by="user")["outcome"], "rebound")
            copy = review.load_receipts(p.root, "shop.1")[-1]
            self.assertEqual((copy["scope"], copy["delta_from"]), ("incremental", r2["delta_from"]))
            self.assertEqual(gate.run(p.root, "shop.1", p.cfg)["verdict"], "pass")

    def test_asked_again_in_review_the_increment_waits_for_the_running_review(self):  # r1 #2
        p = Project(self)
        p.cfg["gate.checks"] = ["true"]
        p.batch()
        p.commit()
        p.run_review(CHANGES)
        p.commit(text="x = 2\n")
        review.request(p.root, "shop.1", p.cfg)
        p.review_output(CHANGES)
        running = review.start(p.root, "shop.1", p.cfg)
        p.commit(text="x = 3\n")
        review.request(p.root, "shop.1", p.cfg)
        with self.assertRaisesRegex(review.FlowError, f"review {running} is still running"):
            review.start(p.root, "shop.1", p.cfg)
        self.assertEqual(p.state(), "review_ready")
        r2 = p.harvest(running)
        p.review_output(APPROVED)
        r3 = p.harvest(review.start(p.root, "shop.1", p.cfg))
        self.assertEqual((r3["round"], r3["scope"], r3["delta_from"]), (3, "incremental", r2["heads"]))
        self.assertEqual(gate.run(p.root, "shop.1", p.cfg)["verdict"], "pass")


class RouteTest(unittest.TestCase):  # REQ-7
    def test_effort_by_tier(self):
        p = Project(self)
        h = p.write_header("shop.1", reqs=["REQ-2"])  # tiers.difficulty S
        goal = state_dir(p.root) / "plans" / "shop" / "goal.md"
        goal.parent.mkdir(parents=True)
        goal.write_text("REQ-1: [防误操作] a\nREQ-2: b\n")

        def effort(cfg, hh=h):
            return review.route(cfg, hh, p.root)[1]

        self.assertEqual(effort({}), "xhigh")  # nothing configured: as before
        self.assertEqual(review.route({"routes.reviewer.effort_s": "low"})[1], "xhigh")  # no header (tick.probes)
        self.assertEqual(review.route({"routes.reviewer.effort": "ultra"})[0], "claude-opus-5-5")  # r1 #3: model only
        cfg = {"routes.reviewer.effort": "high", "routes.reviewer.effort_m": "low"}
        self.assertEqual(effort(cfg), "high")  # effort_m is another tier
        cfg["routes.reviewer.effort_s"] = "medium"
        self.assertEqual(effort(cfg), "medium")
        cfg["routes.reviewer.effort_security"] = "max"
        self.assertEqual(effort(cfg), "medium")  # REQ-2 is not guarded
        self.assertEqual(effort(cfg, {**h, "reqs": ["REQ-2", "REQ-1"]}), "max")
        goal.write_text("REQ-2: [防对抗] b\n")
        self.assertEqual(effort(cfg), "max")
        goal.unlink()
        self.assertEqual(effort(cfg), "max")  # an unreadable goal.md counts as guarded
        self.assertEqual(effort({"routes.reviewer.effort_s": "medium"}), "medium")  # effort_security unset

    def test_bad_effort_never_starts(self):
        p = Project(self)
        p.batch()
        p.commit()
        review.request(p.root, "shop.1", p.cfg)
        p.cfg["routes.reviewer.effort_s"] = "ultra"
        with self.assertRaisesRegex(review.FlowError, "'ultra'"):
            review.start(p.root, "shop.1", p.cfg)
        self.assertEqual((p.state(), p.events("review_started"), p.claude_calls()), ("review_ready", [], []))
        p.cfg["routes.reviewer.effort_s"] = "medium"
        r = p.harvest(review.start(p.root, "shop.1", p.cfg))
        argv = p.claude_calls()[-1]["argv"]
        self.assertEqual((argv[argv.index("--effort") + 1], r["effort"]), ("medium", "medium"))


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
        self.assertEqual(review.diagnose([r("new"), r("new", scope="incremental"), r("repeat", scope="incremental")]),
                         ["escalate_fixer"])  # REQ-6: incremental rounds count
        rebound = {**r(verdict="approved"), "rebound_from": {}}  # `foremind update`'s copy, not a round (§7.6)
        self.assertEqual(review.diagnose([r("new"), rebound, r("repeat")]), [])
        self.assertEqual(review.diagnose([r("new"), r("new"), r(verdict="approved")]), [])
        self.assertEqual(review.diagnose([r("new"), r("repeat")], max_rounds=2), ["escalate_fixer"])
        # N8: only the full rounds since the latest approved receipt count (a rebound copy is one too)
        ok = r(verdict="approved")
        self.assertEqual(review.diagnose([r("new"), r("new"), ok, r("new"), r("repeat")]), [])
        self.assertEqual(review.diagnose([r("new"), ok, r("new"), r("new"), r("repeat")]), ["escalate_fixer"])
        self.assertEqual(review.diagnose([r("new"), r("new"), rebound, r("new"), r("repeat")]), [])


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


class RequestChangesTest(unittest.TestCase):
    def test_controller_sends_an_approved_batch_back(self):
        p = Project(self)
        p.batch(state="approved")
        path = state_dir(p.root) / "batches" / "shop.1.md"
        h, _ = hdr.parse(path.read_text())
        path.write_text(hdr.render(h, "说明\n\n## 状态\n旧状态\n"))
        # no holder: waits in changes_requested for the supervisor's successor, the items in the status section
        self.assertEqual(review.request_changes(p.root, "shop.1", ["补测试", "改名\n再加注释"], by="c1"),
                         "changes_requested")
        body = hdr.parse(path.read_text())[1]
        self.assertTrue(body.startswith("说明\n\n## 状态\n总控要求修改（c1，"))
        self.assertTrue(body.endswith("\n- 补测试\n- 改名\n- 再加注释\n"))
        self.assertNotIn("旧状态", body)
        self.assertEqual(p.events("changes_requested_by")[-1]["items"], ["补测试", "改名", "再加注释"])
        # a holder: the list in its inbox, running again; the section is capped at 40 lines
        review.set_state(p.root, "shop.1", "running")
        for to in ("review_ready", "in_review"):
            review.set_state(p.root, "shop.1", to)
        lock.acquire(p.root, "shop.1", "fm-shop-shop.1-2")
        with mock.patch("foremind.inbox.append", side_effect=OSError("disk full")), self.assertRaises(OSError):
            review.request_changes(p.root, "shop.1", ["x"], by="c1")  # not sent: nothing changed, retry works
        self.assertEqual(p.state(), "in_review")
        self.assertEqual(review.request_changes(p.root, "shop.1", [f"条 {i}" for i in range(50)], by="c1"), "running")
        [m] = inbox.pending_messages("fm-shop-shop.1-2", root=p.root)
        self.assertEqual(m.sender, "controller")
        status = hdr.parse(path.read_text())[1].split("## 状态\n", 1)[1]
        self.assertEqual(len(status.splitlines()), 40)
        self.assertIn("另有 12 行", status)
        with self.assertRaisesRegex(review.FlowError, "running -> changes_requested"):
            review.request_changes(p.root, "shop.1", ["x"], by="c1")

    def test_cli_is_the_controllers_or_the_users(self):
        p = Project(self)
        p.batch(state="approved")
        with mock.patch.dict(os.environ, {"FOREMIND_SESSION": "fm-shop-shop.1-1", "FOREMIND_ROLE": "seat"}):
            code, _, err = cli(["review", "shop.1", "--request-changes", "--item", "x"])
        self.assertEqual((code, p.state()), (1, "approved"))
        self.assertIn("controller", err)
        code, _, err = cli(["review", "shop.1", "--request-changes"])
        self.assertEqual((code, p.state()), (1, "approved"))
        with mock.patch.dict(os.environ, {"FOREMIND_SESSION": "fm-ctl-1", "FOREMIND_ROLE": "controller"}):
            code, out, _ = cli(["review", "shop.1", "--request-changes", "--item", "补测试"])
        self.assertEqual((code, json.loads(out)["state"]), (0, "changes_requested"))
        self.assertEqual(p.events("changes_requested_by")[-1]["by"], "fm-ctl-1")


RAW_CLAUDE = r'''
import json, os, sys
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "role": os.environ.get("FOREMIND_ROLE")}) + "\n")
print(open(os.environ["FAKE_REVIEW"]).read())
'''


def raw_claude(p):
    """The fake claude prints the review file as its whole stdout: tests write `claude -p` result objects."""
    (p.tmp / "bin" / "claude").write_text(f"#!{sys.executable}\n{RAW_CLAUDE}")


def result(out=APPROVED, usd=1.0, **kw):
    """What `claude -p --output-format json --json-schema` prints (REQ-13)."""
    return {"type": "result", "subtype": "success", "is_error": False, "result": "done.", "structured_output": out,
            "total_cost_usd": usd, "usage": {"input_tokens": 10, "output_tokens": 5}, "num_turns": 3, **kw}


class CostTest(unittest.TestCase):  # REQ-13
    def test_argv_flags_only_when_configured(self):
        p = Project(self)
        p.batch()
        p.commit()
        p.run_review()
        argv = p.claude_calls()[-1]["argv"]
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), review.REVIEW_SCHEMA)
        self.assertNotIn("--max-budget-usd", argv)
        self.assertNotIn("--exclude-dynamic-system-prompt-sections", argv)
        self.assertLess(argv.index("--json-schema"), argv.index("--"))
        _, argv, _ = oneshot.prepare(p.root, "decider", p.cfg, {})
        self.assertNotIn("--exclude-dynamic-system-prompt-sections", argv)
        p.cfg.update({"review.max_budget_usd": 2.5, "oneshot.exclude_dynamic_prompt": True})
        p.commit(text="x = 2\n")
        p.run_review()
        argv = p.claude_calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "2.5")
        self.assertIn("--exclude-dynamic-system-prompt-sections", argv[:argv.index("--")])
        _, argv, _ = oneshot.prepare(p.root, "decider", p.cfg, {})
        self.assertIn("--exclude-dynamic-system-prompt-sections", argv[:argv.index("--")])

    def test_structured_output_and_cost_are_recorded(self):
        p = Project(self)
        raw_claude(p)
        p.batch()
        p.commit()
        review.request(p.root, "shop.1", p.cfg)
        p.review_output(result(CHANGES, usd=1.25))
        session = review.start(p.root, "shop.1", p.cfg)
        r = p.harvest(session)
        self.assertEqual((r["verdict"], r["issues"][0]["summary"]), ("changes_requested", "bug"))
        want = {"cost_usd": 1.25, "input_tokens": 10, "cache_write_tokens": None, "cache_read_tokens": None,
                "output_tokens": 5, "num_turns": 3}  # null where not reported
        ev = p.events("review_receipt")[-1]
        self.assertEqual({k: ev[k] for k in want}, want)
        meta = json.loads((state_dir(p.root) / "reviews" / session / "meta.json").read_text())
        self.assertEqual({k: meta[k] for k in want}, want)
        self.assertNotIn("cost_usd", r)  # the receipt schema is unchanged (m2d.5)

    def test_stopped_at_the_budget_is_the_models_failure(self):
        p = Project(self)
        raw_claude(p)
        p.batch()
        p.commit()
        review.request(p.root, "shop.1", p.cfg)
        p.review_output({"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "num_turns": 7,
                         "total_cost_usd": 2.51, "usage": {}, "errors": ["Reached maximum budget ($2.5)"]})
        for want in ("review_ready", "failed"):  # counted against review.max_failures 2
            session = review.start(p.root, "shop.1", p.cfg)
            with self.assertRaisesRegex(review.FlowError, "max_budget_usd"):
                p.harvest(session)
            self.assertEqual(p.state(), want)
        e = p.events("review_failed")[-1]
        self.assertEqual((e["reason"], e["infra"], e["cost_usd"], e["num_turns"]), ("budget", False, 2.51, 7))
        self.assertEqual(review.spent(review.all_events(p.root), "shop.1"), 5.02)

    def test_budget_text_counts_only_under_a_cap(self):  # r1 #7
        err = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                          "result": "max_tokens must be greater than thinking.budget_tokens"})
        self.assertFalse(review.cost(err)[1])
        self.assertTrue(review.cost(err, capped=True)[1])
        self.assertTrue(review.cost(json.dumps({"subtype": "error_max_budget_usd", "is_error": True}))[1])
        self.assertEqual(review.cost("not json"), (dict.fromkeys(review.COST_KEYS), False))

    def test_batch_cap_fails_a_round_that_asks_for_changes(self):  # A5
        p = Project(self)
        raw_claude(p)
        p.batch()  # difficulty S
        p.cfg["review.cost_cap_usd_s"] = 2
        p.commit()
        p.run_review(result(CHANGES, usd=1.5))
        self.assertEqual(p.state(), "changes_requested")
        p.commit(text="x = 2\n")
        r = p.run_review(result(CHANGES, usd=0.5))  # 2.0 reaches the cap
        self.assertEqual(p.state(), "failed")
        last = p.events("batch_state")[-1]
        self.assertEqual((last["reason"], last["cost_usd"], last["heads"]), ("review_cost_cap", 2.0, r["heads"]))
        nc = p.events("review_not_converging")[-1]
        self.assertEqual((nc["round"], nc["reason"], nc["cost_usd"], nc["cap"], nc["actions"]), (2, "cost_cap", 2.0, 2.0, []))
        self.assertEqual(review.harvest(p.root, "shop.1", r["reviewer_session"], p.cfg), r)  # idempotent
        self.assertEqual(len(p.events("review_not_converging")), 1)
        # the user's `foremind run` starts the sum over; an approving round never fails on cost
        review.events(p.root).append("run_requested", batches=["shop.1"])
        for to in ("ready", "running"):
            review.set_state(p.root, "shop.1", to)
        p.commit(text="x = 3\n")
        p.run_review(result(CHANGES, usd=1.0))
        self.assertEqual(p.state(), "changes_requested")
        p.commit(text="x = 4\n")
        p.run_review(result(APPROVED, usd=5.0))
        self.assertEqual(p.state(), "in_review")


class TokenCapTest(unittest.TestCase):  # m2e REQ-2
    USE = {"usage": {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                     "output_tokens": 5}}  # all four reported: result()'s leave two out, such a review is uncounted

    def test_token_cap_and_the_dollar_one_first(self):
        p = Project(self)
        raw_claude(p)
        p.batch()  # difficulty S; each review 10 input + 5 output = 35 input-equivalent tokens
        p.cfg["review.cost_cap_tokens_s"] = 70
        p.commit()
        p.run_review(result(CHANGES, usd=1.0, **self.USE))
        self.assertEqual(p.state(), "changes_requested")
        review.events(p.root).append("review_failed", batch="shop.1", reviewer_session="x", heads={}, error="e",
                                     infra=True, timed_out=False)  # never started: no usage, counted as uncounted
        review.events(p.root).append("review_receipt", batch="shop.1", round=9, verdict="approved",
                                     rebound_from={"api": "x"})  # a rebound copy: no review of its own
        self.assertEqual(review.spent_tokens(review.all_events(p.root), "shop.1"), (35, 1))
        p.commit(text="x = 2\n")
        r = p.run_review(result(CHANGES, usd=0.5, **self.USE))  # 70 reaches the cap
        self.assertEqual(p.state(), "failed")
        last = p.events("batch_state")[-1]
        self.assertEqual({k: last.get(k) for k in ("reason", "unit", "tokens", "cost_usd", "heads")},
                         {"reason": "review_cost_cap", "unit": "tokens", "tokens": 70, "cost_usd": 1.5,
                          "heads": r["heads"]})
        nc = p.events("review_not_converging")[-1]
        self.assertEqual({k: nc.get(k) for k in ("reason", "unit", "tokens", "cap", "cost_usd", "uncounted",
                                                           "actions")},
                         {"reason": "cost_cap", "unit": "tokens", "tokens": 70, "cap": 70.0, "cost_usd": 1.5,
                          "uncounted": 1, "actions": []})
        # both reached in one round: dollars
        review.events(p.root).append("run_requested", batches=["shop.1"])
        for to in ("ready", "running"):
            review.set_state(p.root, "shop.1", to)
        p.cfg.update({"review.cost_cap_tokens_s": 35, "review.cost_cap_usd_s": 1})
        p.commit(text="x = 3\n")
        p.run_review(result(CHANGES, usd=1.0, **self.USE))
        nc = p.events("review_not_converging")[-1]
        self.assertEqual((nc["unit"], nc["cost_usd"], nc["cap"], "tokens" in nc), ("usd", 1.0, 1.0, False))
        self.assertEqual(p.events("batch_state")[-1]["unit"], "usd")

    def test_quota_readings_before_and_after(self):
        p = Project(self)
        raw_claude(p)
        self.enterContext(mock.patch.dict(os.environ, {"FOREMIND_CONFIG_HOME": str(p.tmp / "cfg")}))
        p.batch()
        p.commit()
        tel = state_dir(p.root) / "telemetry"
        tel.mkdir(parents=True, exist_ok=True)
        now = review.time.time()

        def line(ts, **w):
            (tel / "s.statusline.json").write_text(json.dumps({"ts": ts, "rate_limits": {
                k: {"used_percentage": pct, "resets_at": at} for k, (pct, at) in w.items()}}))
        line(now, five_hour=(42, now + 3600), seven_day=(10, now - 1))  # the 7d window has reset since
        p.run_review(result(CHANGES))
        want = {"five_hour_pct": 42.0, "seven_day_pct": None, "ts": now}
        self.assertEqual(p.events("review_started")[-1]["quota_start"], want)
        self.assertEqual(p.events("review_receipt")[-1]["quota_end"], want)
        line(now - 16 * 60, five_hour=(42, now + 3600), seven_day=(10, now + 3600))  # older than quota.stale_min
        self.assertEqual(review.reading(p.root, p.cfg), {"five_hour_pct": None, "seven_day_pct": None,
                                                        "ts": now - 16 * 60})
        self.assertEqual(review.reading(p.root, {**p.cfg, "quota.stale_min": 20})["seven_day_pct"], 10.0)
        line(now, five_hour=(50, now + 3600), seven_day=(11, now + 3600))
        p.review_output(result(CHANGES, usd=0.2))
        out = review.ab(p.root, "shop.1", p.cfg, "low")  # the comparison: ab_review and ab.json
        both = {"five_hour_pct": 50.0, "seven_day_pct": 11.0, "ts": now}
        self.assertEqual((out["quota_start"], out["quota_end"]), (both, both))
        ab = json.loads((state_dir(p.root) / "reviews" / out["reviewer_session"] / "ab.json").read_text())
        self.assertEqual((ab["quota_start"], p.events("ab_review")[-1]["quota_end"]), (both, both))
        (tel / "s.statusline.json").unlink()
        self.assertEqual(review.reading(p.root, p.cfg), {"five_hour_pct": None, "seven_day_pct": None, "ts": None})


class RunTest(unittest.TestCase):
    def test_timeout_is_a_flow_error_and_the_module_default_applies(self):  # M1-7 SF-4
        with self.assertRaisesRegex(review.FlowError, "no answer within 0.2 s"):
            review.run(["sleep", "5"], ".", timeout=0.2)
        with mock.patch.object(review, "TIMEOUT_S", 0.2), self.assertRaises(review.FlowError):
            review.git(".", "-c", "alias.nap=!sleep 5", "nap")
        self.assertEqual(review.run(["true"], ".").returncode, 0, "no bound by default")


if __name__ == "__main__":
    unittest.main()
