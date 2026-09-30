"""Builders shared by the test_plan_* modules, and model.spec's own test."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foremind.plan import model
from foremind.plan.model import Doc, Plan


def header(bid, owns, deps=(), **kw):
    h = {"id": bid, "plan_id": bid.split(".")[0], "reqs": ["REQ-1"],
         "repos": sorted({q.split(":")[0] for q in owns}), "owns_paths": list(owns), "reads": [],
         "depends_on": list(deps), "merge_after": [], "start_commands": [], "accept_commands": ["python3 -m unittest"],
         "tiers": {"difficulty": "M", "org": "exec_review", "review": "zero_context", "model": "claude-opus-5-5",
                   "effort": "high", "reason": "测试用"},
         "mode": "auto", "hard_block": [], "budget_estimate": "50000", "must_read": [], "tools": []}
    h.update(kw)
    return h


def goal_doc(text):
    return Doc({"frozen_at": "2026-09-25T00:00:00+00:00", "sha256": model.text_hash(text)}, text)


def make_plan(plan_id, headers, goal="REQ-1: 做一件事\n", body="计划说明\n", approved=False):
    """`approved`: as if `plan approve` ran (approved_at, batches without a state become planned); no event."""
    g = goal_doc(goal)
    doc = Doc({"plan_id": plan_id, "goal_hash": g.header["sha256"], "batches": [h["id"] for h in headers],
               "revisions": []}, body)
    if approved:
        doc.header |= {"approved_at": "2026-09-25T00:00:00+00:00", "approved_by": "user"}
        headers = [{"state": "planned", **h} for h in headers]
    return Plan(plan_id, doc, {h["id"]: Doc(h, "## 状态\n") for h in headers}, g)


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / ".foremind").mkdir()
        (self.root / "foremind.toml").write_text('[[repos]]\nid = "main"\npath = "."\n')
        self.cfg_home = self.root / "cfg"
        self.enterContext(mock.patch.dict(os.environ, {"FOREMIND_PROJECT": str(self.root),
                                                       "FOREMIND_CONFIG_HOME": str(self.cfg_home)}))

    def save(self, plan):
        model.write_goal(self.root, plan.id, plan.goal)
        model.write(self.root, plan)
        return plan


class SpecTest(unittest.TestCase):
    def test_status_inside_a_fence_is_text(self):
        body = "做什么\n```\n## 状态\n```\n~~~~md\n## 状态\n~~~\n仍在块里\n~~~~\n中间\n## 状态\n席位写的\n"
        self.assertEqual(model.spec(body), "做什么\n```\n## 状态\n```\n~~~~md\n## 状态\n~~~\n仍在块里\n~~~~\n中间\n")
        self.assertEqual(model.spec("a\n## 状态 \t\nb"), "a\n")
        self.assertEqual(model.spec("## 状态"), "")
        # a fence open to the end fences nothing: the section set_state appends after it is cut, every time
        self.assertEqual(model.spec("a\n## 状态说明\n```\n## 状态\n"), "a\n## 状态说明\n```\n")
        self.assertEqual(model.spec("a\n````\nb\n```\n## 状态\nx\n## 状态\ny\n"), "a\n````\nb\n```\n")
        self.assertEqual(model.spec("a\n```\nb"), "a\n```\nb")
        self.assertEqual(model.spec("a\r\n## 状态\r\n"), "a\r\n## 状态\r\n")  # as before: only \n ends a line
