"""Gate end to end on temp projects: request -> reviewer -> gate, with the known pitfalls (DESIGN §7.3)."""
import contextlib
import io
import json
import shutil
import unittest
from pathlib import Path

from foremind import acceptance, gate, review, schemas
from foremind.cli import main
from foremind.fsutil import file_lock, sha256_bytes
from foremind.paths import state_dir
from foremind.schemas import EXAMPLES
from test_gate_fixture import TIERS, Project, sh

MERGE = 'git -C "$FOREMIND_REPO_PATH" merge -q --no-ff -m merge "$FOREMIND_HEAD"'


def ci_run(i, conclusion, status="completed"):
    return {"id": i, "name": "test", "status": status, "conclusion": conclusion}


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
        (sd / "exemptions" / "Q-1.json").write_text(json.dumps(
            {**EXAMPLES["exemption"], "batch": "shop.1", "match": {"paths": ["api:pyproject.toml"]}}))
        decision = {**EXAMPLES["pending"], "id": "Q-1", "state": "answered", "answer": 1, "blocks": ["shop.1"]}
        (sd / "decisions" / "Q-1.json").write_text(json.dumps({**decision, "category": 3}))
        self.assertEqual(self.failing(self.gate(p)), ["dependencies"])  # declared dev (#3), changed runtime
        (sd / "decisions" / "Q-1.json").write_text(json.dumps({**decision, "category": 4}))
        self.assertEqual(self.gate(p)["verdict"], "pass")


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
