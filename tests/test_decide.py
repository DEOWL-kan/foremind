import contextlib
import io
import json
import unittest
from unittest import mock

from foremind import cli, inbox
from foremind.commands import decide as decide_cmd
from foremind.decide import exemption, pending, table
from foremind.supervisor import ready
from test_hooks import BATCH, SESSION, HookBase


class Recorder:
    name = "rec"

    def __init__(self):
        self.sent = []

    def send(self, title, body, priority="P1"):
        self.sent.append((title, body))
        return True


class TableTest(unittest.TestCase):
    def test_conservative_only_moves_controller_and_decider_rows_to_the_user(self):
        self.assertEqual({k for k, r in table.BALANCED.items() if r.locked}, table.LOCKED)
        moved = {k for k in range(24) if table.CONSERVATIVE[k] != table.BALANCED[k]}
        self.assertEqual(moved, {3, 5, 8, 10, 13, 15, 16})
        for k in moved:
            self.assertEqual(table.CONSERVATIVE[k], table.BALANCED[k]._replace(owner="user"))
        self.assertEqual(table.owner({}, 3), "decider")
        self.assertEqual(table.owner({"authz.preset": "conservative"}, 23), "rule")
        for p in ("bogus", ["balanced"], None):  # unknown or not a string: the strictest
            self.assertEqual(table.owner({"authz.preset": p}, 3), "user")
        for d in (None, 4, [4, "5"], [True], [24]):  # unset or malformed: refused
            with self.assertRaisesRegex(ValueError, "hands_off_delegate"):
                table.row({"authz.preset": "hands_off", "authz.hands_off_delegate": d}, 4)

    def test_hands_off_moves_only_the_listed_unlocked_user_rows_to_the_decider(self):
        cfg = {"authz.preset": "hands_off", "authz.hands_off_delegate": [4, 6, 8, 11, 23]}
        moved = {k for k in range(24) if table.row(cfg, k) != table.BALANCED[k]}
        self.assertEqual(moved, {4, 11})  # 6 🔒, 8 the controller's, 23 the rule's: unchanged
        self.assertEqual(table.row(cfg, 4), table.BALANCED[4]._replace(owner="decider"))
        self.assertEqual(table.owner({**cfg, "authz.hands_off_delegate": []}, 3), "decider")  # balanced as is


class DecideTest(HookBase):
    def setUp(self):
        super().setUp()
        self.ntfy = Recorder()
        self.enterContext(mock.patch("foremind.notify.get", return_value=self.ntfy))

    def as_user(self):
        self.use_env({**self.base_env, "FOREMIND_PROJECT": str(self.root)})

    def as_seat(self):
        self.use_env(self.seat_env)

    def decide(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cli.main(["decide", *argv])
        return rc, out.getvalue()

    def q(self, qid="Q-1"):
        return pending.load(self.root, qid)

    def decisions(self):
        return [json.loads(p.read_text()) for p in (self.state / "decisions").glob("Q-*.json")]

    def inbox_texts(self):
        return [m.text for m in inbox.pending_messages(SESSION, root=self.root)]

    def expire(self, qid):
        p = self.state / "exemptions" / f"{qid}.json"
        p.write_text(json.dumps({**json.loads(p.read_text()), "expires_at": "2026-01-01T00:00:00+00:00"}))

    def new(self, *extra, category="4"):
        return self.decide("--new", "--question", "加一个运行时依赖？", "--option", "批准", "--option", "不批准",
                           "--recommended", "2", "--reason", "要用它解析", "--category", category, *extra)

    def test_hit_opens_one_pending_blocks_batch_and_notifies_without_path(self):
        self.assert_denied(self.write(self.wt / "package.json"), "#4", "已登记待决请求", "（Q-1）")
        self.assert_denied(self.write(self.wt / "package.json"))  # same key, still unresolved: reused
        self.assertEqual(self.ntfy.sent, [])  # never pushed from the hook (I38): one detached job, for the new Q-n
        [((jobs, argv), kw)] = self.jobs.call_args_list
        self.assertEqual((jobs, argv[-3:], kw["cwd"]), (self.state / "jobs", ["decide", "--notify", "Q-1"], self.root))
        self.assertEqual((kw["env"]["FOREMIND_PROJECT"], kw["env"]["FOREMIND_SESSION"]), (str(self.root), ""))
        self.assertEqual(self.decide("--notify", "Q-1"), (0, "Q-1：已推送\n"))
        self.assertEqual(self.decide("--notify", "Q-1")[0], 0)  # pushed once per Q-n
        q = self.q()
        self.assertEqual((q["state"], q["blocks"], q["category"], q["recommended"], q["reversible"]),
                         ("open", [BATCH], 4, 2, True))
        self.assertEqual(q["question"], f"批次 {BATCH} 请求 #4：api:package.json")
        self.assertEqual([d["id"] for d in self.decisions()], ["Q-1"])
        self.assertTrue((self.state / "decisions" / "Q-1.md").read_text().startswith("# Q-1（open）"))
        self.assertEqual(ready.blocking(BATCH, self.decisions()), ["Q-1"])
        [(title, body)] = self.ntfy.sent
        self.assertEqual(body, f"Q-1\n1. 批准（本批范围内放行）【选此项即签发放行凭据】\n2. 不批准\n校验码 {q['code']}")
        self.assertNotIn("package", title + body)
        [ev] = self.events("pending_created")
        self.assertEqual(ev["request"]["match"], {"paths": [str(self.wt / "package.json"), "api:package.json"]})
        self.assertEqual((ev["request"]["kind"], ev["request"]["session"], ev["request"]["approve_option"]),
                         ("hit", SESSION, 1))
        self.assertEqual(self.events("pending_routed"), [])

    def test_approve_issues_narrow_exemption_and_tells_holder(self):
        self.assert_denied(self.bash("npm install left-*"))
        self.as_user()
        rc, out = self.decide("Q-1", "1", "--note", "只这一个")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.q()["state"], self.q()["answer"]), ("applied", 1))
        ex = json.loads((self.state / "exemptions" / "Q-1.json").read_text())
        self.assertEqual((ex["batch"], ex["category"], ex["match"]), (BATCH, 4, {"commands": ["npm install left-[*]"]}))
        self.assertEqual(self.events("pending_answered")[0]["note"], "只这一个")
        self.assertIn("Q-1 已答复：批准（本批范围内放行）；已签发放行凭据", self.inbox_texts()[-1])
        self.as_seat()
        self.assertIsNone(self.bash("npm install left-*"))
        self.assert_denied(self.bash("npm install left-pad evil"))  # the legacy `left-*` glob would release it
        self.assertEqual(self.q("Q-2")["state"], "open")

    def test_path_exemption_is_repo_qualified_only(self):
        self.write(self.wt / "package.json")
        self.as_user()
        self.assertEqual(self.decide("Q-1", "1")[0], 0)
        ex = json.loads((self.state / "exemptions" / "Q-1.json").read_text())
        self.assertEqual(ex["match"], {"paths": ["api:package.json"]})
        self.assertEqual(exemption.narrow({"paths": ["/abs/x"], "tools": ["mcp__a[1]"]}), {"tools": ["mcp__a[[]1]"]})
        with self.assertRaisesRegex(ValueError, "仓库之外"):
            exemption.apply_answer(self.root, self.q(), {"batch": BATCH, "match": {"paths": ["/abs/x"]}})
        self.as_seat()
        self.assertIsNone(self.write(self.wt / "package.json"))

    def test_reject_applies_without_exemption_and_a_new_hit_asks_again(self):
        self.bash("npm install left-pad")
        self.as_user()
        rc, out = self.decide("Q-1", "2")
        self.assertEqual((rc, self.q()["state"]), (0, "applied"), out)
        self.assertFalse((self.state / "exemptions" / "Q-1.json").exists())
        self.assertEqual(self.inbox_texts()[-1], "Q-1 已答复：不批准；无程序动作")
        self.assertNotEqual(self.decide("Q-1", "1")[0], 0)  # applied is final
        self.as_seat()
        self.assert_denied(self.bash("npm install left-pad"))
        self.assertEqual(self.q("Q-2")["state"], "open")

    def test_void_and_user_only(self):
        self.bash("npm install left-pad")
        rc, out = self.decide("Q-1", "1")
        self.assertEqual(rc, 1)
        self.assertIn("只归用户", out)
        self.assertEqual(self.decide("--void", "Q-1", "--reason", "x")[0], 1)
        self.as_user()
        self.assertEqual(self.decide("--void", "Q-1")[0], 1)  # a reason is required
        rc, out = self.decide("--void", "Q-1", "--reason", "换了做法")
        self.assertEqual((rc, self.q()["state"]), (0, "void"), out)
        self.assertEqual(self.inbox_texts()[-1], "Q-1 已作废：换了做法")
        self.assertEqual(self.decide("Q-1", "1")[0], 1)
        self.assertEqual(self.decide("--void", "Q-1", "--reason", "again")[0], 1)
        self.assertEqual(ready.blocking(BATCH, self.decisions()), [])
        self.assertEqual((self.decide("--notify", "Q-1")[0], self.ntfy.sent), (0, []))  # resolved: not pushed

    def test_new_kinds_and_refusals(self):
        rc, out = self.new("--category", "1")
        self.assertEqual(rc, 1)
        self.assertIn("D 记录", out)
        self.assertEqual(self.decide("--new", "--question", "q", "--option", "a", "--recommended", "1",
                                     "--reason", "r", "--category", "11")[0], 1)  # one option: schema
        self.assertIn("--approve-option", self.new("--path", "api:requirements.txt")[1])
        self.assertIn("<仓库>:<路径>", self.new("--path", "requirements.txt", "--approve-option", "1")[1])
        self.assertFalse((self.state / "decisions").exists() and list((self.state / "decisions").iterdir()))

        rc, out = self.new("--path", "api:requirements.txt", "--approve-option", "1")
        self.assertEqual(rc, 0, out)
        self.assertIn("Q-1 已登记（hit）", out)
        req = pending.request(self.root, "Q-1")
        self.assertEqual(req, {"kind": "hit", "category": 4, "batch": BATCH, "session": SESSION,
                               "match": {"paths": ["api:requirements.txt"]}, "approve_option": 1,
                               "key": decide_cmd.key(4, BATCH, {"paths": ["api:requirements.txt"]})})
        self.assertEqual(self.q()["blocks"], [BATCH])
        self.assertIn("1. 批准【选此项即签发放行凭据】", out)
        self.assertIn("scope", self.new("--category", "8", "--path", "api:docs/x.md", "--approve-option", "1")[1])
        self.assertIn("manual", self.new("--category", "11", "--blocks", "auth.3", "--blocks", "auth.4")[1])
        self.assertIn("Q-4", self.new("--category", "22", "--command", "vim ~/.claude/settings.json",
                                      "--approve-option", "1")[1])
        self.assertFalse(self.q("Q-4")["reversible"])  # 🔒

        self.as_user()
        rc, out = self.decide()
        self.assertEqual(rc, 0, out)
        self.assertEqual([line.split()[0] for line in out.splitlines()], ["Q-3", "Q-1", "Q-2", "Q-4"])
        lines = dict(line.split(maxsplit=1) for line in out.splitlines())
        self.assertTrue(lines["Q-1"].endswith("选项 1【选此项即签发放行凭据】"), lines["Q-1"])
        self.assertTrue(lines["Q-2"].endswith("选项 1【选此项即扩大本批范围】"), lines["Q-2"])
        self.assertNotIn("【", lines["Q-3"])  # manual: no answer triggers a program action
        with mock.patch.dict(pending.HANDLERS, {"scope": "foremind.decide._not_written_yet"}):
            rc, out = self.decide("Q-2", "1")
        self.assertEqual((rc, self.q("Q-2")["state"]), (0, "answered"), out)
        self.assertIn("这一类待决还没有程序动作", out)
        self.assertEqual(self.decide("Q-2", "2")[0], 1)  # answered: only a same-answer retry
        rc, out = self.decide("Q-3", "1")
        self.assertEqual((rc, self.q("Q-3")["state"]), (0, "applied"), out)
        self.assertIn("无程序动作", out)
        self.assertIn("请求：", self.decide("show", "Q-1")[1])

    def test_apply_failure_keeps_answer_and_retries(self):
        self.bash("npm install left-pad")
        self.as_user()
        with mock.patch.object(exemption, "apply_answer", side_effect=RuntimeError("disk full")):
            rc, out = self.decide("Q-1", "1")
        self.assertEqual((rc, self.q()["state"]), (0, "answered"), out)
        self.assertIn("disk full", self.events("pending_apply_failed")[0]["error"])
        self.assertEqual([q["id"] for q in pending.unresolved(self.root)], ["Q-1"])
        self.as_seat()
        self.assert_denied(self.bash("npm install left-pad"))  # answered, not applied: reused, not asked again
        self.assertEqual(([d["id"] for d in self.decisions()], self.jobs.call_count), (["Q-1"], 1))
        self.as_user()
        self.assertEqual(self.decide("Q-1", "1")[0], 0)  # same answer again: apply retried
        self.assertEqual(self.q()["state"], "applied")
        self.assertTrue((self.state / "exemptions" / "Q-1.json").exists())

    def test_delegated_rows_are_deciding_and_not_pushed_until_escalated(self):
        for cat, to in (("3", "decider"), ("13", "controller")):
            rc, out = self.new("--category", cat)
            self.assertEqual(rc, 0, out)
            self.assertIn("（deciding）", out)
            self.assertEqual(self.events("pending_routed")[-1]["to"], to)
        self.assertEqual([self.q(q)["state"] for q in ("Q-1", "Q-2")], ["deciding", "deciding"])
        self.assertEqual(ready.blocking(BATCH, self.decisions()), ["Q-1", "Q-2"])  # still in the way
        self.assertEqual((self.decide("--notify", "Q-1"), self.ntfy.sent), ((0, "Q-1：不再推送（推送过或已了结）\n"), []))
        pending.escalate(self.root, "Q-1", "决策者：置信度 low")
        self.assertEqual((self.q()["state"], self.events("pending_escalated")[0]["reason"]),
                         ("escalated", "决策者：置信度 low"))
        [(title, _)] = self.ntfy.sent
        self.assertEqual(title, "待决 Q-1")
        with self.assertRaisesRegex(ValueError, "不能上交"):
            pending.escalate(self.root, "Q-1", "again")
        with self.assertRaisesRegex(ValueError, "不是 deciding"):  # a delegate answers only what it was given
            pending.answer(self.root, "Q-1", 1, by="decider", expect="deciding")
        self.as_user()
        self.assertEqual(self.decide("Q-1", "2")[0], 0)  # the user answers an escalated one
        self.assertEqual(self.q()["state"], "applied")
        self.user_config('[authz]\npreset = "conservative"\n')
        self.as_seat()
        self.assertEqual(self.new("--category", "3")[0], 0)
        self.assertEqual((self.q("Q-3")["state"], len(self.events("pending_routed")), len(self.ntfy.sent)),
                         ("open", 2, 2))

    def test_confirm_overturn_and_restore_are_the_users(self):  # m2b.4
        for f in ("requirements.txt", "setup.py"):  # REQ-16: the same path twice would be the same request
            self.new("--category", "3", "--path", f"api:{f}", "--approve-option", "1")
        for q in ("Q-1", "Q-2"):
            pending.answer(self.root, q, 1, by="decider", expect="deciding", provisional=True)
        self.assertTrue((self.state / "exemptions" / "Q-2.json").exists())
        self.assertIn("Q-2 暂定（PV-2）：批准", self.inbox_texts()[-1])
        pending._log(self.root).append("delegation_tightened", category=3, step=1)
        for argv in (("Q-1", "--confirm"), ("Q-2", "--overturn"), ("--restore", "3")):
            rc, out = self.decide(*argv)
            self.assertEqual(rc, 1)
            self.assertIn("只归用户", out)
        self.as_user()
        self.assertIn("暂定决定：foremind decide Q-1 --confirm | --overturn", self.decide("show", "Q-1")[1])
        for argv in (("Q-1", "--confirm", "--overturn"), ("--confirm",), ("Q-1", "1", "--confirm")):
            self.assertIn("用法", self.decide(*argv)[1])
        self.assertEqual(self.decide("Q-1", "--confirm"), (0, "Q-1 已确认\n"))
        self.assertEqual(self.decide("Q-2", "--overturn", "--note", "换个库")[0], 0)
        self.assertEqual((self.q("Q-1")["state"], self.q("Q-2")["state"]), ("confirmed", "overturned"))
        self.assertFalse((self.state / "exemptions" / "Q-2.json").exists())
        self.assertIn("被用户推翻", self.inbox_texts()[-1])
        self.assertEqual(self.decide("Q-2", "--confirm")[0], 1)  # overturned is not provisional any more
        self.assertEqual(self.decide("--restore", "3"), (0, "#3 的委托已恢复（原收紧到第 1 级）\n"))
        self.assertEqual(self.decide("--restore", "3")[0], 1)

    def test_the_same_new_request_is_reused_and_once_undone_is_the_users(self):  # REQ-16
        expire = self.expire
        ask = lambda *paths, cat="3": self.new("--category", cat, *(x for f in paths for x in ("--path", f)),
                                               "--approve-option", "1")
        self.assertEqual(ask("api:a.txt", "api:b.txt")[0], 0)
        rc, out = ask("api:b.txt", "api:a.txt", "api:a.txt")  # same paths, another order: the same request
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith("同一请求已有未决的 Q-1，沿用它"), out)
        self.assertIn("Q-2 已登记", ask("api:a.txt")[1])  # a subset: another request
        self.assertIn("Q-3 已登记", ask("api:a.txt", "api:b.txt", cat="4")[1])  # another category
        self.assertIn("Q-4 已登记", self.new("--category", "11")[1])
        self.assertIn("Q-5 已登记", self.new("--category", "11")[1])  # no path or command: no key, never merged
        self.assertNotEqual(decide_cmd.key(3, BATCH, {"paths": ["api:a.txt"]}),
                            decide_cmd.key(3, "auth.9", {"paths": ["api:a.txt"]}))  # another batch
        # the decider's provisional answer overturned / voided: the same request again goes to the user
        for q, undo in (("Q-1", lambda: pending.conclude(self.root, "Q-1", "overturned")),
                        ("Q-2", lambda: pending.void(self.root, "Q-2", "换个做法"))):
            pending.answer(self.root, q, 1, by="decider", expect="deciding", provisional=True)
            undo()
        self.assertIn("（open）", ask("api:a.txt", "api:b.txt")[1])
        self.assertIn("（open）", ask("api:a.txt")[1])
        self.assertIn("（deciding）", ask("api:c.txt")[1])  # not undone before
        pending.answer(self.root, "Q-8", 1, by="decider", expect="deciding", provisional=True)
        for _ in range(2):  # r1: provisional, then overdue, is still in effect: reused, no second PV
            self.assertTrue(ask("api:c.txt")[1].startswith("同一请求已有未决的 Q-8，沿用它"))
            pending.overdue(self.root, "Q-8")
        expire("Q-8")  # r2: its exemption expired, the overdue Q-n no longer stands for it: asked anew
        self.assertIn("Q-9 已登记", out := ask("api:c.txt")[1])
        self.assertIn("（deciding）", out)
        with mock.patch.object(exemption, "apply_answer", side_effect=RuntimeError("disk full")):
            pending.answer(self.root, "Q-9", 1, by="decider", expect="deciding", provisional=True)
            for n in range(2):  # no exemption, but its failed action is retried (supervisor.seat_retries = 2)
                self.assertTrue(ask("api:c.txt")[1].startswith("同一请求已有未决的 Q-9"), n)
                pending.apply(self.root, "Q-9")
        self.assertIn("Q-10 已登记", ask("api:c.txt")[1])  # retries used up

    def test_a_hit_on_an_overdue_request_whose_exemption_expired_asks_again(self):  # REQ-16, r2
        expire = self.expire
        self.user_config('[authz]\npreset = "hands_off"\nhands_off_delegate = [4]\n')
        self.assert_denied(self.write(self.wt / "package.json"), "（Q-1）")
        self.assertEqual(self.q()["state"], "deciding")
        pending.answer(self.root, "Q-1", 1, by="decider", expect="deciding", provisional=True)
        pending.overdue(self.root, "Q-1")
        self.assertIsNone(self.write(self.wt / "package.json"))  # released by its exemption
        expire("Q-1")
        self.assert_denied(self.write(self.wt / "package.json"), "（Q-2）")
        self.assertEqual((self.q()["state"], self.q("Q-2")["state"]), ("overdue", "deciding"))

    def test_deferred_push_is_told_as_recorded(self):  # REQ-16
        self.user_config('[notify]\np1 = "report"\n')
        rc, out = self.new("--category", "11")
        self.assertEqual(rc, 0, out)
        self.assertIn("Q-1 已登记（manual），已记下，进下一次推送或运行报告", out)
        self.assertEqual((self.ntfy.sent, self.events("notify_unsent")), ([], []))
        self.new("--category", "3", "--path", "api:a.txt", "--approve-option", "1", "--blocks", "nobody.1")
        pending.answer(self.root, "Q-2", 1, by="decider", expect="deciding", provisional=True)
        with mock.patch.object(pending, "_add_status", return_value="running"):  # no batch file in this fixture
            self.as_user()
            rc, out = self.decide("Q-2", "--overturn")
        self.assertEqual(rc, 0, out)
        self.assertIn("已记下，进下一次推送或运行报告", out)
        self.assertNotIn("推送没发出", out)
        self.user_config('[notify]\nchannel = "ntfy"\n')
        self.ntfy.send = lambda *a: False  # a real failure still says so
        self.as_seat()
        self.assertIn("推送没发出（已记 notify_unsent）", self.new("--category", "12")[1])

    def test_list_shows_provisional_and_overdue_with_their_deadline(self):  # REQ-17
        for f in ("a", "b"):
            self.new("--category", "3", "--path", f"api:{f}.txt", "--approve-option", "1")
            pending.answer(self.root, f"Q-{1 + (f == 'b')}", 1, by="decider", expect="deciding", provisional=True)
        pending.overdue(self.root, "Q-2")
        self.new("--category", "11")
        pvs = pending.provisionals(self.root)
        self.as_user()
        lines = dict(line.split(maxsplit=1) for line in self.decide()[1].splitlines())
        self.assertEqual(sorted(lines), ["Q-1", "Q-2", "Q-3"])
        self.assertTrue(lines["Q-1"].startswith(f"provisional  挡 1 批  暂定 PV-1 到期 {pvs['Q-1']['deadline']}  #3"))
        self.assertTrue(lines["Q-2"].startswith(f"overdue  挡 1 批  暂定 PV-2 到期 {pvs['Q-2']['deadline']}"))
        self.assertTrue(lines["Q-3"].startswith(f"open  挡 1 批  期限 {self.q('Q-3')['deadline']}"))
        self.decide("Q-1", "--confirm")
        self.assertNotIn("Q-1", self.decide()[1])
        self.assertEqual([q["id"] for q in pending.unresolved(self.root)], ["Q-3"])  # other readers: unchanged

    def test_hands_off_refused_and_unreadable_config_reads_as_conservative(self):
        self.user_config('[authz]\npreset = "hands_off"\n')
        rc, out = self.new()
        self.assertEqual(rc, 1)
        self.assertIn("hands_off_delegate", out)
        self.assert_denied(self.bash("npm install left-pad"), "#4", "登记待决失败", "decide --new")  # deny stands
        self.assertEqual((self.decisions(), self.ntfy.sent), ([], []))
        self.user_config("[authz\n")
        rc, out = self.new("--category", "3")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.events("pending_routed"), [])  # conservative: #3 is the user's own
        self.assertIn("decide --notify Q-1", out)  # not pushed, the key left unhandled
        self.assertEqual((self.decide("--notify", "Q-1")[0], self.ntfy.sent, self.events("notify")), (1, [], []))
        self.user_config("")
        self.assertEqual((self.decide("--notify", "Q-1")[0], len(self.ntfy.sent)), (0, 1))

    def test_unreleasable_hit_opens_no_pending(self):
        unfiled = ("未登记待决", "decide --new")
        self.assert_denied(self.write(self.tmp / "other" / "CLAUDE.md"), "#22", "所有仓库之外", *unfiled)
        self.write_header(owns_paths=["api:src/*"])
        reason = self.assert_denied(self.write(self.wt / "package.json"), "#4", "可写矩阵", *unfiled, "owns_paths")
        self.assertNotIn("已登记待决请求", reason)
        self.assertEqual(len(self.events("pending_needed")), 2)
        self.assertEqual((self.decisions(), self.ntfy.sent, self.jobs.call_count), ([], [], 0))

    def test_new_blocks_the_held_batch_over_a_stale_env(self):
        self.use_env({**self.seat_env, "FOREMIND_BATCH": "auth.9"})  # after a continuation
        self.assertEqual(self.new("--category", "11")[0], 0)
        self.assertEqual((self.q()["blocks"], pending.request(self.root, "Q-1")["batch"]), ([BATCH], BATCH))

    def test_bookkeeping_failure_keeps_the_deny(self):
        with mock.patch.object(pending, "from_hit", side_effect=OSError("read-only")):
            self.assert_denied(self.bash("npm install left-pad"), "#4", "登记待决失败")
        self.assertIn("read-only", self.events("hook_error")[-1]["error"])
        self.assertEqual(len(self.events("pending_needed")), 1)
        self.jobs.side_effect = OSError("fork failed")  # no push job: the Q-n still stands
        self.assert_denied(self.bash("npm install left-pad"), "已登记待决请求", "（Q-1）")
        self.assertIn("fork failed", self.events("hook_error")[-1]["error"])


if __name__ == "__main__":
    unittest.main()
