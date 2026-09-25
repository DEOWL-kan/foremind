import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from foremind.supervisor import ready, stuck

M = 60


def h(bid, state="planned", owns=None, deps=(), prior=None):
    d = {"id": bid, "state": state, "owns_paths": owns or [f"main:{bid}"], "depends_on": list(deps)}
    if prior:
        d["state_prior"] = prior
    return d


class ReadyTest(unittest.TestCase):
    def test_dependencies_follow_the_mode(self):
        hs = {"p.1": h("p.1", "approved"), "p.2": h("p.2", deps=["p.1"])}
        self.assertEqual(ready.why_not_ready("p.2", hs, [], "approved"), [])
        self.assertEqual(ready.why_not_ready("p.2", hs, [], "merged"), [("after", "p.1")])
        self.assertEqual(ready.why_not_ready("p.2", hs, [], "bogus"), [("after", "p.1")], "unknown mode = merged")
        hs["p.1"]["state"] = "cataloged"
        self.assertEqual(ready.why_not_ready("p.2", hs, [], "merged"), [])

    def test_only_unanswered_decisions_block(self):
        qs = [{"id": "Q-1", "blocks": ["p.1"], "state": "open"},
              {"id": "Q-2", "blocks": ["p.1"], "state": "answered", "answer": 1},
              {"id": "Q-3", "blocks": ["p.9"], "state": "escalated"}]
        self.assertEqual(ready.why_not_ready("p.1", {"p.1": h("p.1")}, qs, "merged"), [("decision", "Q-1")])

    def test_overlap_with_started_or_busy_batches_but_not_along_depends_on(self):
        hs = {"p.1": h("p.1", "running", ["main:src/"]), "p.2": h("p.2", owns=["main:src/a.py"]),
              "p.3": h("p.3", owns=["main:src/b.py"], deps=["p.1"]), "p.4": h("p.4", owns=["main:doc/x"]),
              "p.5": h("p.5", owns=["main:doc/x/y.md"]), "p.6": h("p.6", "blocked", ["other:src/"], prior="ready")}
        self.assertEqual(ready.why_not_ready("p.2", hs, [], "merged"), [("overlaps", "p.1")])
        self.assertEqual(ready.why_not_ready("p.3", hs, [], "merged"), [("after", "p.1")], "I43: no overlap check")
        self.assertEqual(ready.why_not_ready("p.5", hs, [], "merged"), [], "p.4 has not started")
        self.assertEqual(ready.why_not_ready("p.5", hs, [], "merged", busy={"p.4"}), [("overlaps", "p.4")])
        hs["p.1"].update(state="stuck", state_prior="running")
        self.assertEqual(ready.why_not_ready("p.2", hs, [], "merged"), [("overlaps", "p.1")], "side branch still holds")

    def test_full_block_follows_waiting_upstreams(self):
        hs = {"p.1": h("p.1", "failed"), "p.2": h("p.2", deps=["p.1"]), "p.3": h("p.3", "ready"),
              "p.4": h("p.4", "merged")}
        direct = {"p.1": "failed", "p.3": "待决 Q-1"}
        why = {"p.2": [("after", "p.1")]}
        self.assertEqual(ready.full_block(hs, direct, why), direct)
        hs["p.5"] = h("p.5", "ready")
        self.assertIsNone(ready.full_block(hs, direct, {**why, "p.5": []}), "p.5 can start")
        hs["p.5"] = h("p.5")
        self.assertIsNone(ready.full_block(hs, direct, {**why, "p.5": [("overlaps", "p.9")]}), "p.9 is not waiting")
        self.assertIsNone(ready.full_block({"p.4": hs["p.4"]}, {}, {}), "nothing unfinished is not a full block")


class StuckTest(unittest.TestCase):
    def judge(self, quiet, done=None, tool_open=False, alive=True, cfg=None):
        return stuck.judge(quiet, 10_000, done or {}, tool_open=tool_open, alive=alive, cfg=cfg or {})

    def test_three_stages(self):
        self.assertIsNone(self.judge(19 * M))
        self.assertEqual(self.judge(20 * M), "remind")
        self.assertIsNone(self.judge(30 * M, {"remind": 10_000 - 10 * M}), "20 minutes after the reminder")
        self.assertEqual(self.judge(41 * M, {"remind": 10_000 - 20 * M}), "ask")
        done = {"remind": 10_000 - 40 * M, "ask": 10_000 - 19 * M}
        self.assertIsNone(self.judge(61 * M, done))
        done["ask"] = 10_000 - 20 * M
        self.assertEqual(self.judge(61 * M, done), "stuck")
        self.assertEqual(self.judge(5 * M, cfg={"stuck.remind_min": 5}), "remind")

    def test_busy_tool_only_reminds_for_two_hours(self):
        self.assertEqual(self.judge(20 * M, tool_open=True), "remind")
        self.assertIsNone(self.judge(100 * M, {"remind": 10_000 - 80 * M}, tool_open=True))
        self.assertEqual(self.judge(120 * M, {"remind": 10_000 - 100 * M}, tool_open=True), "ask")

    def test_a_session_the_carrier_reports_gone_is_stuck_at_once(self):
        self.assertEqual(self.judge(0, alive=False), "stuck")
        self.assertIsNone(self.judge(0, alive=None), "unknown to the carrier is not gone")

    def test_last_change_sees_commits_and_uncommitted_files(self):
        repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
               "GIT_COMMITTER_DATE": "@1700000000 +0000", "GIT_AUTHOR_DATE": "@1700000000 +0000"}
        run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], env=env, check=True, capture_output=True)  # noqa: E731
        run("init", "-q")
        (repo / "a").write_text("1")
        run("add", "a")
        run("commit", "-qm", "a")
        os.utime(repo / "a", (1600000000, 1600000000))
        self.assertEqual(stuck.last_change(repo), 1700000000)
        (repo / "new").write_text("x")
        os.utime(repo / "new", (1800000000, 1800000000))
        self.assertEqual(stuck.last_change(repo), 1800000000)
        self.assertIsNone(stuck.last_change(repo / "missing"))


if __name__ == "__main__":
    unittest.main()
