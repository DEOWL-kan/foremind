"""Gate end to end on temp projects: request -> reviewer -> gate, with the known pitfalls (DESIGN §7.3)."""
import contextlib
import io
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from foremind import acceptance, gate, review, schemas
from foremind.cli import main
from foremind.fsutil import file_lock, sha256_bytes
from foremind.paths import state_dir
from foremind.schemas import EXAMPLES
from test_gate_fixture import TIERS, Project, sh

MERGE = 'git -C "$FOREMIND_REPO_PATH" merge -q --no-ff -m merge "$FOREMIND_HEAD"'


def ci_run(i, conclusion, status="completed", name="test"):
    return {"id": i, "name": name, "status": status, "conclusion": conclusion}


class Base(unittest.TestCase):
    def project(self, repos=("api",), *, origin=False, cfg=None, **header):
        p = Project(self, repos, origin=origin)
        p.batch(**header)
        for r in repos:
            p.commit(r)
        p.cfg.update({"gate.checks": ["true"], **(cfg or {})})
        return p

    def gate(self, p, **kw):
        return gate.run(p.root, "shop.1", p.cfg, **kw)

    def check(self, res, name):
        return next(c for c in res["checks"] if c["name"] == name)

    def failing(self, res):
        return sorted(c["name"] for c in res["checks"] if not c["ok"])

    def posted(self, p):
        return [a for a in p.gh_log() if a[:1] == ["api"] and "-X" in a]


class HappyPathTest(Base):
    def test_done_level_is_delivered(self):
        p = self.project()
        p.run_review()
        res = self.gate(p)
        self.assertEqual((res["verdict"], self.failing(res), res["state"]), ("pass", [], "delivered"))
        names = {c["name"] for c in res["checks"]}
        self.assertLessEqual({"receipt", "reviewer", "receipt_event", "acceptance", "checks", "owns_paths",
                              "merge_after", "dependencies", "delivery", "ci:api"}, names)
        self.assertNotIn("role", names)  # done level: nothing to merge
        stored = json.loads(Path(res["path"]).read_text())
        self.assertEqual(schemas.validate("gate_result", stored), [])
        self.assertEqual(stored["heads"], {"api": p.head()})
        self.assertEqual([e["state"] for e in p.events("batch_state")][-2:], ["approved", "delivered"])
        self.assertEqual(p.gh_log(), [])  # local repo without a remote: no host calls
        self.assertEqual(self.gate(p)["state"], "delivered")
        with self.assertRaisesRegex(review.FlowError, "nothing new"):
            review.request(p.root, "shop.1", p.cfg)
        p.commit(text="x = 3\n")  # a new head voids the approval: next round
        self.assertEqual(review.request(p.root, "shop.1", p.cfg)["state"], "review_ready")
        self.assertEqual([e["state"] for e in p.events("batch_state")][-3:],
                         ["changes_requested", "running", "review_ready"])

    def test_cli(self):
        p = self.project()
        p.run_review()
        (p.root / "foremind.toml").write_text('gate.checks = ["true"]\n\n[[repos]]\nid = "api"\npath = "api"\n\n'
                                              '[delivery.repo.api]\ntarget_branch = "main"\n')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["gate", "shop.1"]), 0)
        self.assertIn("pass · shop.1 is delivered", out.getvalue())
        with (state_dir(p.root) / "events.jsonl").open("a") as f:
            f.write("{torn line\n")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main(["gate", "shop.1"]), 2)  # still a refusal, as one line instead of a traceback
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertIn("corrupt JSON", err.getvalue())

    def test_one_gate_per_batch(self):
        p = self.project()
        p.run_review()
        with file_lock(state_dir(p.root) / "batches" / "shop.1.gate.lock"):
            with self.assertRaisesRegex(review.FlowError, "another gate run"):
                self.gate(p)
        self.assertEqual(p.state(), "in_review")
        self.assertEqual(self.gate(p)["state"], "delivered")

    def test_result_without_its_run_record_is_rerun(self):
        p = self.project()
        p.run_review()
        self.gate(p)
        path = acceptance.result_path(p.root, "shop.1", "accept", {"api": p.head()})
        path.write_text(json.dumps(json.loads(path.read_text())))  # same result, other bytes: no run event matches
        res = self.gate(p)
        self.assertEqual((res["verdict"], len(p.events("accept_run"))), ("pass", 2))

    def test_result_without_repo_is_rerun(self):
        p = self.project()
        p.run_review()
        self.gate(p)
        path = acceptance.result_path(p.root, "shop.1", "accept", {"api": p.head()})
        old = json.loads(path.read_text())
        del old["commands"][0]["repo"]
        data = json.dumps(old)
        path.write_text(data)  # as an older version wrote it, with its run record
        review.events(p.root).append("accept_run", batch="shop.1", heads=old["heads"], path=path.name,
                                     sha256=sha256_bytes(data.encode()), ok=True)
        res = self.gate(p)
        self.assertEqual((res["verdict"], len(p.events("accept_run"))), ("pass", 3))
        self.assertEqual(json.loads(path.read_text())["commands"][0]["repo"], "api")

    def test_same_command_in_another_repo_is_not_the_same_result(self):
        in_api = 'test "$(basename "$(pwd -P)")" = api'
        p = self.project(("api", "app"), cfg={"gate.checks": [{"run": in_api, "repo": "api"}]})
        p.run_review()
        self.assertEqual(self.gate(p)["state"], "delivered")
        p.cfg["gate.checks"] = [{"run": in_api, "repo": "app"}]  # same text, run elsewhere
        res = self.gate(p)
        self.assertEqual(self.failing(res), ["checks"])
        self.assertIn("failed", self.check(res, "checks")["detail"])
        self.assertEqual(len(p.events("checks_run")), 2)


class PitfallTest(Base):
    def forge(self, p, **changes):
        """Rewrite the latest receipt *with* a matching event, so only the SHA rules can catch it."""
        n, path = review.receipts(p.root, "shop.1")[-1]
        r = {**json.loads(path.read_text()), **changes}
        data = json.dumps(r)
        path.write_text(data)
        review.events(p.root).append("review_receipt", batch="shop.1", round=n, path=path.name,
                                     sha256=sha256_bytes(data.encode()), verdict=r["verdict"],
                                     reviewer_session=r["reviewer_session"])

    def test_abbreviated_or_uppercase_sha(self):
        p = self.project()
        p.run_review()
        for bad in (p.head()[:7], p.head().upper()):
            self.forge(p, heads={"api": bad})
            res = self.gate(p)
            self.assertIn("does not match", self.check(res, "receipt")["detail"])
            self.assertEqual((res["verdict"], p.state()), ("fail", "in_review"))

    def test_receipt_commit_moves_head(self):
        p = self.project()
        p.run_review()
        shutil.copy(review.receipts(p.root, "shop.1")[-1][1], p.wt() / "src" / "receipt.json")
        sh(p.wt(), "git", "add", "-A")
        sh(p.wt(), "git", "commit", "-q", "-m", "add receipt")
        res = self.gate(p)
        self.assertIn("differ from the current heads in ['api']", self.check(res, "receipt")["detail"])
        self.assertEqual(p.state(), "in_review")

    def test_rebase_voids_receipt_even_with_same_patch(self):
        p = self.project()
        p.run_review()
        old = p.head()
        (p.root / "api" / "other.txt").write_text("target moved\n")
        sh(p.root / "api", "git", "add", "-A")
        sh(p.root / "api", "git", "commit", "-q", "-m", "target moves")
        sh(p.wt(), "git", "rebase", "-q", "main")
        self.assertNotEqual(p.head(), old)
        self.assertEqual(gate.patch_id(p.wt(), f"{old}~1...{old}"), gate.patch_id(p.wt(), "main...HEAD"))
        res = self.gate(p)  # rebinding is the program's update path (M2-2), never the gate's
        self.assertFalse(self.check(res, "receipt")["ok"])

    def test_same_sha_red_then_green(self):
        p = Project(self)
        green = p.tmp / "green"
        p.batch(accept_commands=[f"test -f {green}"])
        p.commit()
        p.cfg["gate.checks"] = ["true"]
        p.run_review()
        self.assertEqual(self.failing(self.gate(p)), ["acceptance"])
        green.touch()
        self.assertEqual(self.failing(self.gate(p)), ["acceptance"])  # the red run stands until rerun
        res = self.gate(p, rerun=True)
        self.assertEqual((res["verdict"], res["state"]), ("pass", "delivered"))

    def test_any_repo_head_change_voids_the_receipt(self):
        p = self.project(("api", "app"))
        p.run_review()
        p.commit("app", text="y = 2\n")
        self.assertIn("in ['app']", self.check(self.gate(p), "receipt")["detail"])

    def test_reviewer_must_not_be_the_implementer(self):
        p = self.project()
        r = p.run_review()
        (state_dir(p.root) / "batches" / "shop.1.lock").write_text(r["reviewer_session"] + "\n")
        res = self.gate(p)
        self.assertEqual(self.failing(res), ["acceptance", "checks", "reviewer"])
        self.assertIn("waits for an approved receipt", self.check(res, "acceptance")["detail"])

    def test_receipt_edited_after_it_was_written(self):
        p = self.project()
        p.run_review()
        path = review.receipts(p.root, "shop.1")[-1][1]
        r = json.loads(path.read_text())
        r["issues"][0]["summary"] = "edited"
        path.write_text(json.dumps(r))
        self.assertEqual(self.failing(self.gate(p)), ["acceptance", "checks", "receipt_event"])

    def test_forged_receipt_needs_a_review_the_program_started(self):
        p = self.project()
        p.run_review()
        self.forge(p, reviewer_session="made-up")  # receipt and receipt event agree, but no review_started
        self.assertEqual(self.failing(self.gate(p)), ["acceptance", "checks", "receipt_event"])

    def test_receipt_of_another_batch_or_delta_without_full(self):
        p = self.project()
        p.run_review()
        self.forge(p, batch="shop.9")
        self.assertIn("receipt is for shop.9", self.check(self.gate(p), "receipt")["detail"])
        self.forge(p, batch="shop.1", scope="delta")
        self.assertIn("delta approval without an approved full review", self.check(self.gate(p), "receipt")["detail"])

    def test_requester_cannot_be_the_reviewer(self):
        p = self.project()
        r = p.run_review()
        review.events(p.root).append("review_requested", batch="shop.1", heads={"api": p.head()}, bases={},
                                     requested_by=r["reviewer_session"], prs={})
        self.assertEqual(self.failing(self.gate(p)), ["acceptance", "checks", "reviewer"])

    def test_forged_heads_and_session_in_model_output_do_not_help(self):
        p = self.project()
        p.run_review({"verdict": "approved", "issues": [], "heads": {"api": "0" * 40}, "reviewer_session": "x"})
        self.assertEqual(self.gate(p)["verdict"], "pass")  # the program's own fields were used


class ScopeTest(Base):
    def test_owns_paths(self):
        p = self.project()
        p.commit(path="docs/notes.md", text="n\n")
        p.run_review()
        res = self.gate(p)
        self.assertIn("api:docs/notes.md", self.check(res, "owns_paths")["detail"])
        self.assertEqual((self.failing(res), p.state()), (["owns_paths"], "approved"))  # approved, not delivered

    def test_merge_after_and_depends_on(self):
        p = Project(self)
        p.write_header("shop.2", state="approved")
        p.batch(merge_after=[{"batch": "shop.2", "reason": "provider first"}], depends_on=["shop.2"])
        p.commit()
        p.cfg["gate.checks"] = ["true"]
        p.run_review()
        self.assertIn("shop.2 (approved)", self.check(self.gate(p), "merge_after")["detail"])
        p.write_header("shop.2", state="merged")
        self.assertEqual(self.gate(p)["state"], "delivered")

    def test_dependency_sections_need_a_matching_decision(self):
        p = self.project(owns_paths=["api:src/*", "api:pyproject.toml"])
        p.commit(path="pyproject.toml", text='[project]\nname = "x"\ndependencies = ["requests"]\n')
        p.run_review()
        self.assertIn("#4", self.check(self.gate(p), "dependencies")["detail"])
        sd = state_dir(p.root)
        (sd / "exemptions").mkdir()
        (sd / "decisions").mkdir()
        ex = {**EXAMPLES["exemption"], "batch": "shop.1", "match": {"paths": ["api:pyproject.toml"]},
              "expires_at": "2999-01-01T00:00:00+00:00"}
        (sd / "exemptions" / "Q-1.json").write_text(json.dumps(ex))
        decision = {**EXAMPLES["pending"], "id": "Q-1", "state": "answered", "answer": 1, "blocks": ["shop.1"]}
        (sd / "decisions" / "Q-1.json").write_text(json.dumps({**decision, "category": 3}))
        self.assertEqual(self.failing(self.gate(p)), ["dependencies"])  # declared dev (#3), changed runtime
        (sd / "decisions" / "Q-1.json").write_text(json.dumps({**decision, "category": 4}))
        self.assertEqual(self.gate(p)["verdict"], "pass")
        (sd / "exemptions" / "Q-1.json").write_text(json.dumps({**ex, "expires_at": "2026-01-08T00:00:00+00:00"}))
        self.assertEqual(self.failing(self.gate(p)), ["dependencies"])  # expired: approves nothing any more


class CiTest(Base):
    def test_none_needs_local_checks(self):
        p = self.project(cfg={"gate.checks": []})
        p.run_review()
        res = self.gate(p)
        self.assertEqual((self.failing(res), p.state()), (["ci:api"], "in_review"))
        p.cfg["gate.checks"] = ["true"]
        self.assertEqual(self.gate(p)["state"], "delivered")

    def test_required_uses_the_latest_run_on_the_sha(self):
        p = self.project(origin=True, cfg={"delivery.repo.api.push_pr": "system", "gate.ci": "required"})
        p.run_review()
        head = p.head()
        p.gh_state(check_runs={head: [ci_run(1, "success"), ci_run(2, "failure")]},
                   statuses={head: [{"id": 9, "context": "foremind/gate", "state": "failure"}]})
        res = self.gate(p)
        self.assertEqual((self.failing(res), p.state()), (["ci:api"], "in_review"))
        self.assertIn("state=failure", self.posted(p)[-1])
        p.gh_state(check_runs={head: [ci_run(1, "success"), ci_run(2, "failure"), ci_run(3, "success")]})
        res = self.gate(p)  # re-run went green on the same SHA; our own old foremind/gate status is ignored
        self.assertEqual((res["verdict"], res["state"]), ("pass", "delivered"))
        post = self.posted(p)[-1]
        self.assertIn(f"repos/{{owner}}/{{repo}}/statuses/{head}", post)
        self.assertIn("context=foremind/gate", post)
        self.assertIn("state=success", post)

    def test_every_page_and_the_required_checks(self):
        p = self.project(origin=True, cfg={"delivery.repo.api.push_pr": "system", "gate.ci": "required"})
        p.run_review()
        head = p.head()
        runs = [ci_run(i, "failure" if i == 140 else "success", name=f"c{i}") for i in range(1, 151)]
        p.gh_state(check_runs={head: runs})
        res = self.gate(p)
        self.assertIn("check c140", self.check(res, "ci:api")["detail"])  # on the second page
        self.assertIn(["api", f"repos/{{owner}}/{{repo}}/commits/{head}/check-runs?per_page=100&page=2"], p.gh_log())
        runs[139]["conclusion"] = "success"
        prop = state_dir(p.root) / "delivery.proposal.json"
        facts = lambda v: json.dumps({"repos": {"api": {"facts": {"required_checks": {"value": v}}}}})  # noqa: E731
        prop.write_text(facts(["c1", "build", "foremind/gate"]))  # foremind/gate: ours, written after the checks
        p.gh_state(check_runs={head: runs})
        res = self.gate(p)
        self.assertIn("not reported yet: ['build']", self.check(res, "ci:api")["detail"])
        self.assertEqual((res["verdict"], p.state()), ("fail", "in_review"))
        self.assertIn("state=pending", self.posted(p)[-1])
        prop.write_text(facts("unknown"))  # unknown: the checks that reported are all there is to go by
        self.assertIsNone(gate.required_checks(p.root, "api"))
        self.assertIsNone(gate.required_checks(p.root, "app"))
        prop.write_text(facts(["c1", "build"]))
        p.gh_state(statuses={head: [{"id": 1, "context": "build", "state": "success"}]})
        self.assertEqual(self.gate(p)["state"], "delivered")

    def test_user_push_needs_local_checks(self):
        p = self.project(cfg={"gate.ci": "required", "gate.checks": []})  # #23 the user's (N13)
        p.run_review()
        res = self.gate(p)
        self.assertEqual((self.failing(res), p.state()), (["ci:api"], "in_review"))
        self.assertIn("[gate].checks is empty", self.check(res, "ci:api")["detail"])
        p.cfg["gate.checks"] = ["true"]
        self.assertEqual(self.gate(p)["state"], "delivered")

    def test_local_first_marks_ready_once_then_waits_for_ci(self):
        p = self.project(origin=True, cfg={"delivery.repo.api.push_pr": "system", "gate.ci": "local_first"})
        p.run_review()
        res = self.gate(p)
        self.assertEqual((res["verdict"], p.state()), ("fail", "approved"))  # local checks carried the rounds
        self.assertIn("state=pending", self.posted(p)[-1])
        p.gh_state(check_runs={p.head(): [ci_run(1, "success")]})
        self.assertEqual(self.gate(p)["state"], "delivered")
        self.assertEqual([a for a in p.gh_log() if a[:2] == ["pr", "ready"]], [["pr", "ready", "fm/shop.1"]])

    def test_user_push_gets_the_status_after_pushing(self):
        p = self.project(origin=True, cfg={"gate.ci": "required"})  # #23 the user's: local checks stand in
        p.run_review()
        res = self.gate(p)
        self.assertEqual((res["verdict"], self.posted(p)), ("pass", []))
        self.assertTrue(Path(res["path"]).exists())  # the local result comes first
        sh(p.wt(), "git", "push", "-q", "origin", "fm/shop.1")
        self.gate(p)
        self.assertIn(f"repos/{{owner}}/{{repo}}/statuses/{p.head()}", self.posted(p)[-1])

    def test_user_pushed_ancestor_is_quiet_diverged_voids_the_receipt(self):
        p = self.project(origin=True)  # #23 the user's
        first = p.head()
        sh(p.wt(), "git", "push", "-q", "origin", "fm/shop.1")  # the user pushed an earlier commit...
        p.commit(text="x = 2\n")  # ...and the seat went on
        p.run_review()
        res = self.gate(p)
        self.assertEqual((res["verdict"], res["warnings"], self.posted(p)), ("pass", [], []))
        theirs = sh(p.wt(), "git", "commit-tree", "-p", first, "-m", "theirs", f"{first}^{{tree}}")
        sh(p.wt(), "git", "push", "-q", "-f", "origin", f"{theirs}:refs/heads/fm/shop.1")  # something else pushed
        res = self.gate(p)
        self.assertEqual((res["verdict"], self.failing(res)), ("fail", ["receipt"]))
        self.assertIn("receipt void", self.check(res, "receipt")["detail"])


class CiLimitTest(Base):
    def required(self):
        p = self.project(origin=True, cfg={"delivery.repo.api.push_pr": "system", "gate.ci": "required"})
        p.run_review()
        return p

    def test_paging_stops_and_the_lock_goes(self):
        p = self.required()
        p.gh_state(check_runs={p.head(): [ci_run(i, "success", name=f"c{i}") for i in range(1, 251)]})
        with mock.patch.object(gate, "MAX_PAGES", 2), self.assertRaisesRegex(review.FlowError, "more than 2 pages"):
            self.gate(p)
        with gate.batch_lock(p.root, "shop.1"):  # released: not LockBusy
            pass
        self.assertEqual(self.gate(p)["state"], "delivered")  # three pages are within the real cap

    def test_pending_ci_is_named_in_the_gate_result(self):  # the update phase times it (ci_pending_long)
        p = self.required()
        p.gh_state(check_runs={p.head(): [ci_run(1, None, status="in_progress")]})
        res = self.gate(p)
        g = p.events("gate_result")[-1]
        self.assertEqual((g["failing"], g["pending"]), (["ci:api"], ["ci:api"]))
        self.assertEqual((res["verdict"], p.state(), p.events("ci_pending_long")), ("fail", "in_review", []))
        self.assertIn("state=pending", self.posted(p)[-1])


class MergeTest(Base):
    def merge_dev(self, **cfg):
        return self.project(cfg={"delivery.level": "merge_dev", "delivery.repo.api.merge_command": MERGE, **cfg})

    def test_merge_command_checked_before_and_after(self):
        p = self.merge_dev()
        p.run_review()
        res = self.gate(p, role="seat")
        self.assertEqual((res["verdict"], res["state"]), ("pass", "merged"))
        base = p.events("review_requested")[-1]["bases"]["api"]
        self.assertTrue(gate.is_merged(p.root / "api", "main", p.head(), base))
        self.assertTrue(gate.batch_merged(p.root, "shop.1", p.cfg))
        self.assertEqual([e["phase"] for e in p.events("merge")], ["intent", "result"])

    def test_merge_command_that_merges_nothing_is_p0(self):
        p = self.merge_dev(**{"delivery.repo.api.merge_command": "true"})
        p.run_review()
        with self.assertRaisesRegex(review.FlowError, "P0"):
            self.gate(p)
        self.assertEqual((p.state(), len(p.events("merge_unverified"))), ("delivered", 1))

    def test_role_and_up_to_date(self):
        p = self.merge_dev()
        p.run_review()
        res = self.gate(p, role="controller")
        self.assertEqual((self.failing(res), p.state()), (["role"], "approved"))
        (p.root / "api" / "other.txt").write_text("target moved\n")
        sh(p.root / "api", "git", "add", "-A")
        sh(p.root / "api", "git", "commit", "-q", "-m", "target moves")
        self.assertEqual(self.failing(self.gate(p, role="seat")), ["up_to_date"])

    def test_gh_merge_matches_the_head(self):
        p = self.project(origin=True, cfg={"delivery.repo.api.push_pr": "system", "delivery.level": "merge_dev",
                                           "delivery.repo.api.merge_method": "squash"})
        p.run_review()
        self.assertEqual(self.gate(p)["state"], "merged")
        self.assertIn(["pr", "merge", "fm/shop.1", "--squash", "--match-head-commit", p.head()], p.gh_log())

    def gh_group(self, *repos):
        cfg = {"delivery.level": "merge_dev"}
        for r in repos:
            cfg.update({f"delivery.repo.{r}.push_pr": "system", f"delivery.repo.{r}.merge_method": "squash"})
        p = self.project(repos, origin=True, cfg=cfg)
        p.run_review()
        return p

    def test_none_merges_while_the_host_finds_one_unmergeable(self):  # N3
        p = self.gh_group("api", "app")
        p.pr_state("app", mergeStateStatus="BLOCKED")
        self.enterContext(mock.patch.object(gate, "SETTLE_S", 0))
        res = self.gate(p)
        self.assertEqual((res["verdict"], self.failing(res), res["state"], p.state()),
                         ("fail", ["mergeable"], "delivered", "delivered"))
        self.assertIn("app BLOCKED", self.check(res, "mergeable")["detail"])
        self.assertNotIn("api", self.check(res, "mergeable")["detail"])
        self.assertEqual([a for a in p.gh_log() if a[:2] == ["pr", "merge"]], [])
        self.assertIn("state=success", self.posted(p)[-1])  # written before the precheck, left alone by it
        stored = json.loads(Path(res["path"]).read_text())
        self.assertEqual((stored["verdict"], p.events("gate_result")[-1]["failing"]), ("fail", ["mergeable"]))
        for status in ("DIRTY", "BEHIND", "DRAFT", "UNKNOWN", "SOMETHING_NEW"):
            p.pr_state("app", mergeStateStatus=status)
            self.assertEqual(self.failing(self.gate(p)), ["mergeable"], status)
        p.pr_state("app", mergeStateStatus="UNSTABLE")  # mergeable; only checks outside the required set are red
        res = self.gate(p)
        self.assertEqual((res["verdict"], res["state"]), ("pass", "merged"))
        self.assertEqual(len([a for a in p.gh_log() if a[:2] == ["pr", "merge"]]), 2)

    def test_a_status_the_host_is_still_working_out_is_asked_again(self):  # r1 #5
        p = self.gh_group("api")
        p.pr_state("api", mergeStateStatus="UNKNOWN")  # right after foremind/gate was written
        waits, real = [], gate.time.sleep

        def sleep(s):
            if s != gate.SETTLE_S:  # the job waits of the run
                return real(s)
            waits.append(s)
            p.pr_state("api", mergeStateStatus="BLOCKED" if len(waits) == 1 else "CLEAN")

        with mock.patch.object(gate.time, "sleep", sleep):
            res = self.gate(p)
        self.assertEqual((res["verdict"], res["state"], len(waits)), ("pass", "merged", 2))
        self.assertEqual(len([a for a in p.gh_log() if "mergeStateStatus" in a]), 3)

    def test_merge_queue_keeps_the_batch_delivered(self):  # N4
        p = self.gh_group("api")
        p.gh_state(merge_queue=True)
        res = self.gate(p)
        self.assertEqual((res["verdict"], res["state"], p.state()), ("pass", "delivered", "delivered"))
        self.assertIn("merge queue", res["warnings"][-1])
        [q] = p.events("merge_queued")
        self.assertEqual((q["batch"], q["repo"], q["head"]), ("shop.1", "api", p.head()))
        self.assertEqual((p.events("merge_unverified"), [e["phase"] for e in p.events("merge")]), ([], ["intent"]))
        calls = len(p.gh_log())
        self.assertEqual(self.gate(p)["state"], "delivered")  # still queued: neither merged again nor prechecked
        again = p.gh_log()[calls:]
        self.assertEqual([a for a in again if a[:2] == ["pr", "merge"] or "mergeStateStatus" in a], [])
        self.assertFalse(gate.batch_merged(p.root, "shop.1", p.cfg))
        p.pr_state(state="MERGED")  # the queue merged it
        self.assertTrue(gate.batch_merged(p.root, "shop.1", p.cfg))  # what L0 reconcile goes by
        self.assertEqual(self.gate(p)["state"], "merged")
        self.assertEqual(len([a for a in p.gh_log() if a[:2] == ["pr", "merge"]]), 1)

    def test_a_queued_pr_that_closed_is_merged_again(self):
        p = self.gh_group("api")
        p.gh_state(merge_queue=True)
        self.gate(p)
        p.pr_state(state="CLOSED")  # no longer waiting in the queue: the next run merges through gh again
        p.gh_state(merge_queue=False)
        res = self.gate(p)
        self.assertEqual((res["state"], len(p.events("merge_queued"))), ("merged", 1))

    def test_group_is_checked_before_any_merge(self):
        p = self.project(("api", "app"), cfg={"delivery.level": "merge_dev", "delivery.repo.api.merge_command": MERGE})
        p.run_review()
        res = self.gate(p)
        self.assertEqual(self.failing(res), ["merge_way"])  # app cannot merge: api is not merged either
        self.assertFalse(gate.batch_merged(p.root, "shop.1", p.cfg))
        p.cfg["delivery.repo.app.merge_command"] = "exit 3"
        with self.assertRaisesRegex(review.FlowError, "exited 3"):
            self.gate(p)
        self.assertEqual(p.events("merge_group_partial")[-1]["merged"], ["api"])
        self.assertEqual(p.state(), "delivered")
        p.cfg["delivery.repo.app.merge_command"] = MERGE  # fixed: the rerun skips api (now behind its target)
        res = self.gate(p)
        self.assertEqual((res["verdict"], res["state"]), ("pass", "merged"))
        self.assertEqual([e["repo"] for e in p.events("merge") if e["phase"] == "result"], ["api", "app"])

    def test_merge_without_a_recorded_base_is_checked_against_the_pre_merge_base(self):
        p = self.merge_dev()
        p.run_review()
        review.events(p.root).append("review_requested", batch="shop.1", heads={"api": p.head()}, bases={},
                                     requested_by="fm-shop-shop.1-1", prs={})  # an older version's event
        self.assertEqual(gate.bases(p.root, "shop.1", *self.pairs_heads(p)), {})
        self.assertEqual(self.gate(p)["state"], "merged")  # not a false P0
        self.assertEqual(p.events("merge_unverified"), [])

    def pairs_heads(self, p):
        pairs = review.batch_repos(p.root, review.load_batch(p.root, "shop.1"), p.cfg)
        return pairs, review.heads(pairs)

    def test_audited_tier_waits_for_audit(self):
        p = self.project(tiers={**TIERS, "org": "controller_seats"})
        p.run_review()
        self.assertEqual(self.gate(p)["state"], "awaiting_audit")

    def test_status_stays_pending_until_the_audit(self):
        """§20 I49: a success status before the audit would let a required check pass and the PR be merged."""
        p = self.project(origin=True, tiers={**TIERS, "org": "controller_seats"},
                         cfg={"delivery.repo.api.push_pr": "system"})
        p.run_review()
        res = self.gate(p)
        self.assertEqual((res["verdict"], res["state"]), ("pass", "awaiting_audit"))
        self.assertIn("state=pending", self.posted(p)[-1])
        self.assertIn("description=awaiting pre-delivery audit", self.posted(p)[-1])
        self.gate(p)  # still waiting: still pending
        self.assertIn("state=pending", self.posted(p)[-1])
        self.assertTrue(gate.pre_delivery_audit(review.load_batch(p.root, "shop.1"), p.cfg))

    def test_merge_prechecks_alone_leave_the_status(self):
        p = self.project(origin=True, cfg={"delivery.repo.api.merge_command": MERGE})
        sh(p.wt(), "git", "push", "-q", "origin", "fm/shop.1")
        p.run_review()
        self.assertEqual(self.gate(p)["state"], "delivered")
        self.assertIn("state=success", self.posted(p)[-1])
        n = len(self.posted(p))
        p.cfg["delivery.level"] = "merge_dev"
        res = self.gate(p, role="controller")  # N9: only the merge failed its checks, the batch did not
        self.assertEqual((self.failing(res), res["state"]), (["role"], "delivered"))
        self.assertEqual(len(self.posted(p)), n)  # the success stands
        p.cfg["gate.checks"] = ["false"]
        self.assertEqual(self.failing(self.gate(p, role="controller")), ["checks", "role"])
        self.assertIn("state=failure", self.posted(p)[-1])

    def test_session_without_a_role_cannot_merge(self):
        p = self.merge_dev()
        p.run_review()
        res = self.gate(p, session="fm-shop-ctl-1")  # FOREMIND_SESSION set, FOREMIND_ROLE missing
        self.assertEqual((self.failing(res), p.state()), (["role"], "approved"))
        self.assertIn("in session fm-shop-ctl-1", self.check(res, "role")["detail"])
        self.assertEqual(self.gate(p)["state"], "merged")  # no session at all: the user

    def test_merge_waits_for_the_reviewed_head_on_the_remote(self):
        push = f'{MERGE} && git -C "$FOREMIND_REPO_PATH" push -q origin main'
        p = self.project(origin=True, cfg={"delivery.level": "merge_dev", "delivery.repo.api.merge_command": push})
        p.run_review()
        res = self.gate(p)  # #23 the user's, not pushed yet: checked before any merge
        self.assertEqual((self.failing(res), p.state()), (["pushed"], "approved"))
        repo = review.batch_repos(p.root, review.load_batch(p.root, "shop.1"), p.cfg)[0][0]
        with self.assertRaisesRegex(review.FlowError, "the reviewed head is"):  # and again right before merging
            gate._merge_one(p.root, "shop.1", repo, p.wt(), p.head(), p.cfg, None)
        self.assertEqual(p.events("merge"), [])
        sh(p.wt(), "git", "push", "-q", "origin", "fm/shop.1")
        self.assertEqual(self.gate(p)["state"], "merged")

    def test_gate_refuses_before_review(self):
        p = self.project()
        with self.assertRaisesRegex(review.FlowError, "running"):
            self.gate(p)


if __name__ == "__main__":
    unittest.main()
