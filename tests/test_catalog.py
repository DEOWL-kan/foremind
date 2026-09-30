import contextlib
import io
import json
from unittest import mock

from foremind import catalog, cli
from foremind.decide import pending
from foremind.fsutil import sha256_file
from test_hooks import HookBase

PREC = {"question": "Q-1", "category": 4, "conclusion": "运行时不引第三方依赖", "scope": "全项目",
        "premises": [{"kind": "manual", "text": "用户答复 Q-1"}], "review_on": "2027-01-01", "supersedes": [],
        "id": "J-99", "needs_review": True, "superseded_by": "J-7"}
IMPROVEMENT = {"source": "m2a.9", "category": "rework", "description": "d", "evidence": ["e"], "premises": []}


class CatalogTest(HookBase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch("foremind.notify.get"))
        self.use_env({**self.base_env, "FOREMIND_PROJECT": str(self.root)})  # the user

    def put(self, rel, text):
        (self.state / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.state / rel).write_text(text)

    def read(self, rel):
        return (self.state / rel).read_text()

    def ask(self, answer=None):
        rec, _ = pending.create(self.root, {}, question="加依赖？", options=["批准", "不批准"], recommended=2,
                                reason="r", category=4, blocks=[], reversible=True, request={"kind": "manual"})
        if answer:
            pending.answer(self.root, rec["id"], answer)
        return rec["id"]

    def apply(self, patch=(), precedents=(), improvements=()):
        return catalog.apply(self.root, {"patch": list(patch), "precedents": list(precedents),
                                         "improvements": list(improvements)}, by="user")

    def test_patches_follow_anchor_rules_and_fail_one_by_one(self):
        self.put("index.md", "# 索引\n\n## 契约\n\n- a → x\n- a 续\n\n### 细项\n\n- deep\n\n## 坑\n\n- b\n- dup\n- dup\n")
        res = self.apply([
            {"file": "index.md", "anchor": "- a → x", "op": "append", "content": "- a2"},  # not a heading
            {"file": "index.md", "anchor": "## 契约", "op": "append", "content": "- c → y"},
            {"file": "index.md", "op": "append", "content": "- 末尾"},
            {"file": "index.md", "anchor": "- b", "op": "replace", "content": "- b2"},
            {"file": "index.md", "anchor": "- deep", "op": "delete", "content": ""},
            {"file": "index.md", "anchor": "- dup", "op": "delete", "content": ""},  # twice: rejected
            {"file": "index.md", "anchor": "- a", "op": "replace", "content": "z"},  # not a whole line: rejected
            {"file": "../config.toml", "op": "append", "content": "x"},  # outside I22: rejected
            {"file": "index/api.md", "op": "append", "content": "- 分册"},
        ])
        self.assertEqual([r["result"] for r in res], ["applied"] * 5 + ["rejected"] * 3 + ["applied"])
        self.assertIn("出现 2 处", res[5]["note"])
        self.assertIn("找不到", res[6]["note"])
        self.assertEqual(self.read("index.md"), "# 索引\n\n## 契约\n\n- a → x\n- a 续\n- a2\n\n### 细项\n\n- c → y\n\n"
                                                "## 坑\n\n- b2\n- dup\n- dup\n- 末尾\n")
        self.assertEqual(self.read("index/api.md"), "- 分册\n")
        [ev] = self.events("catalog_applied")
        self.assertEqual(ev["files"], {"index.md": sha256_file(self.state / "index.md"),
                                       "index/api.md": sha256_file(self.state / "index/api.md")})
        self.assertEqual(ev["by"], "user")

    def test_rules_replace_becomes_user_pending_and_applies_on_approval(self):
        patch = {"file": "rules.md", "anchor": "- 禁止联网安装", "op": "replace", "content": "- 可联网安装"}
        self.assertIn("找不到", self.apply([patch])[0]["note"])  # no rules.md yet: rejected, not a pending
        self.put("rules.md", "# 规则\n\n- 禁止联网安装\n")
        res = self.apply([{**patch, "anchor": "- 没有这行"}, {**patch, "op": "delete", "content": "", "anchor": ""}])
        self.assertEqual([r["result"] for r in res], ["rejected", "rejected"])  # bad anchor: no pending at all
        self.assertIn("找不到", res[0]["note"])
        self.assertFalse((self.state / "decisions").exists())
        [res] = self.apply([patch])
        self.assertEqual((res["result"], res["question"]), ("pending", "Q-1"))
        self.assertEqual(self.apply([patch])[0]["question"], "Q-1")  # same patch while unresolved: same Q-n
        self.assertEqual(self.read("rules.md"), "# 规则\n\n- 禁止联网安装\n")
        q, req = pending.load(self.root, "Q-1"), pending.request(self.root, "Q-1")
        self.assertEqual((q["category"], q["options"], q["reversible"]), (22, ["应用这条修改", "不应用"], False))
        self.assertEqual((req["kind"], req["approve_option"], req["patch"]), ("catalog", 1, patch))
        self.assertEqual((q["question"], req["original"]), ("编目员要替换 rules.md 的一行：- 禁止联网安装", "- 禁止联网安装\n"))
        self.assertIn("已改 rules.md", pending.answer(self.root, "Q-1", 1))
        self.assertEqual(self.read("rules.md"), "# 规则\n\n- 可联网安装\n")
        self.assertEqual(pending.load(self.root, "Q-1")["state"], "applied")
        self.assertEqual(self.events("catalog_applied")[-1]["question"], "Q-1")
        [appended] = self.apply([{"file": "rules.md", "op": "append", "content": "- 新规则"}])
        self.assertEqual(appended["result"], "applied")  # append is written directly

    def test_precedents_need_a_user_answer_and_supersede_through_the_user(self):
        self.ask()  # Q-1 open
        [res] = self.apply(precedents=[PREC])
        self.assertIn("Q-1 当前是 open", res["note"])
        pending.answer(self.root, "Q-1", 1)
        res = self.apply(precedents=[PREC, {**PREC, "conclusion": "第二条"}, {**PREC, "question": "Q-9"},
                                     {**PREC, "supersedes": ["J-5"]}])
        self.assertEqual([r["result"] for r in res], ["applied", "applied", "rejected", "rejected"])
        self.assertEqual([r["note"] for r in res[:2]], ["J-1", "J-2"])
        j1 = catalog._precedents(self.root)["J-1"]
        self.assertEqual((j1["id"], j1["needs_review"], "superseded_by" in j1), ("J-1", False, False))
        self.assertTrue(self.read("precedents.md").startswith("## J-1\n\n```json\n"))
        [res] = self.apply(precedents=[{**PREC, "conclusion": "新结论", "supersedes": ["J-1"]}])
        self.assertEqual(res["result"], "pending")
        q = pending.load(self.root, res["question"])
        self.assertEqual(q["category"], 4)
        self.assertNotIn("J-3", self.read("precedents.md"))
        pending.answer(self.root, q["id"], 1)
        got = catalog._precedents(self.root)
        self.assertEqual((got["J-3"]["conclusion"], got["J-3"]["supersedes"], got["J-1"]["superseded_by"]),
                         ("新结论", ["J-1"], "J-3"))
        self.assertNotIn("superseded_by", got["J-2"])

    def test_rules_section_pending_names_the_section_and_quotes_it(self):
        rules = "".join(f"- r{n}\n" for n in range(catalog.ORIGINAL_LINES + 5))
        self.put("rules.md", f"# 规则\n\n## 安装\n\n{rules}\n## 其他\n\n- x\n")
        [res] = self.apply([{"file": "rules.md", "anchor": "## 安装", "op": "delete", "content": ""}])
        q, req = pending.load(self.root, res["question"]), pending.request(self.root, res["question"])
        n = catalog.ORIGINAL_LINES + 8  # heading, blank, the rules, trailing blank
        self.assertEqual(q["question"], f"编目员要删除 rules.md 的整个小节（{n} 行）：## 安装")
        before = self.read("rules.md").replace("- r44\n", "- r44 改过\n")  # past the quoted 40 lines
        self.put("rules.md", before)
        self.assertIn("原文在建待决后变了", pending.answer(self.root, q["id"], 1))
        self.assertEqual((pending.load(self.root, q["id"])["state"], self.read("rules.md")), ("answered", before))
        self.assertTrue(req["original"].startswith("## 安装\n\n- r0\n"))
        self.assertTrue(req["original"].endswith(f"…（共 {n} 行，只附前 {catalog.ORIGINAL_LINES} 行）"))

    def test_heading_anchor_replace_and_delete_act_on_the_whole_section(self):
        self.put("index.md", "# 索引\n\n## 旧\n\n- o1\n- o2\n\n### 子\n\n- s\n\n## 留\n\n- k\n")
        res = self.apply([{"file": "index.md", "anchor": "## 旧", "op": "replace", "content": "## 新\n\n- n"}])
        self.assertEqual(res[0]["result"], "applied")
        self.assertEqual(self.read("index.md"), "# 索引\n\n## 新\n\n- n\n\n## 留\n\n- k\n")
        self.apply([{"file": "index.md", "anchor": "## 新", "op": "delete", "content": ""}])
        self.assertEqual(self.read("index.md"), "# 索引\n\n## 留\n\n- k\n")

    def set_state(self, qid, st):
        rec = {**pending.load(self.root, qid), "state": st, "answer": 1}
        pending._save(self.root, rec, pending.request(self.root, qid))

    def test_precedent_sources_per_i32(self):
        self.ask()
        self.set_state("Q-1", "confirmed")  # a confirmed provisional counts
        self.assertEqual(self.apply(precedents=[PREC])[0]["note"], "J-1")
        self.ask()
        pending.answer(self.root, "Q-2", 1, by="decider")  # answered, but not by the user
        [res] = self.apply(precedents=[{**PREC, "question": "Q-2"}])
        self.assertIn("不是用户答复的（decider）", res["note"])
        [res] = self.apply(precedents=[{**PREC, "conclusion": "新", "supersedes": ["J-1"]}])
        req = pending.request(self.root, res["question"])
        self.assertNotIn("id", req["precedent"])  # no placeholder J-n shown to the user
        self.assertNotIn("needs_review", req["precedent"])
        self.set_state("Q-1", "overturned")  # overturned before the user approves the supersede
        self.assertIn("执行失败", pending.answer(self.root, res["question"], 1))
        self.assertEqual(pending.load(self.root, res["question"])["state"], "answered")
        self.assertEqual(list(catalog._precedents(self.root)), ["J-1"])
        self.assertNotIn("superseded_by", catalog._precedents(self.root)["J-1"])

    def test_a_superseded_precedent_cannot_be_superseded_again(self):  # m2a.10 r3 note
        self.ask(answer=1)
        self.apply(precedents=[PREC])  # J-1
        newer = lambda c: {**PREC, "conclusion": c, "supersedes": ["J-1"]}
        q2, q3 = (self.apply(precedents=[newer(c)])[0]["question"] for c in ("二", "三"))  # both while J-1 stands
        self.assertIn("已写判例 J-2", pending.answer(self.root, q2, 1))
        self.assertIn("不能取代已被取代的判例：J-1（已被 J-2 取代）", pending.answer(self.root, q3, 1))  # on approval
        self.assertEqual((pending.load(self.root, q3)["state"], list(catalog._precedents(self.root))),
                         ("answered", ["J-1", "J-2"]))
        [res] = self.apply(precedents=[newer("四")])  # and before any pending
        self.assertEqual(res["result"], "rejected")
        self.assertIn("已被 J-2 取代", res["note"])
        self.put("precedents.md", "## J-1\n\n```json\n[1]\n```\n")
        [res] = self.apply(precedents=[newer("五")])
        self.assertEqual((res["result"], res["note"]), ("rejected", "判例 J-1 不是 JSON 对象"))

    def test_rules_replace_without_original_hash_is_refused(self):  # m2a.10 r6 note: fail-closed
        self.put("rules.md", "# 规则\n\n- 禁止联网安装\n")
        patch = {"file": "rules.md", "anchor": "- 禁止联网安装", "op": "delete", "content": ""}
        rec, _ = pending.create(self.root, {}, question="删？", options=catalog.OPTIONS, recommended=2, reason="r",
                                category=22, blocks=[], reversible=False,
                                request={"kind": "catalog", "approve_option": 1, "patch": patch})
        self.assertIn("没有原文哈希（original_sha256）", pending.answer(self.root, rec["id"], 1))
        self.assertEqual((pending.load(self.root, rec["id"])["state"], self.read("rules.md")),
                         ("answered", "# 规则\n\n- 禁止联网安装\n"))

    def test_improvements_and_bad_shapes(self):
        res = self.apply(improvements=[IMPROVEMENT, {**IMPROVEMENT, "evidence": []}])
        self.assertEqual([r["result"] for r in res], ["applied", "rejected"])
        self.assertIn('"description": "d"', self.read("improvements.md"))
        res = catalog.apply(self.root, {"patch": {}, "extra": []}, by="controller")
        self.assertEqual(sorted((r["part"], r["result"]) for r in res), [("extra", "rejected"), ("patch", "rejected")])

    def run_cli(self, env, path):
        self.use_env({**self.base_env, "FOREMIND_PROJECT": str(self.root), **env})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            return cli.main(["catalog", "apply", str(path)]), out.getvalue()

    def test_command_roles(self):
        f = self.tmp / "out.json"
        f.write_text(json.dumps({"patch": [{"file": "capabilities.md", "op": "append", "content": "- x"}],
                                 "precedents": [], "improvements": []}))
        for env in ({"FOREMIND_SESSION": "s", "FOREMIND_ROLE": "seat"}, {"FOREMIND_ROLE": "planner"},
                    {"FOREMIND_SESSION": "s"}):
            rc, out = self.run_cli(env, f)
            self.assertEqual(rc, 1, env)
            self.assertIn("只有用户", out)
        self.assertFalse((self.state / "capabilities.md").exists())
        self.assertEqual(self.run_cli({"FOREMIND_ROLE": "supervisor"}, f), (0, "patch[0] applied：capabilities.md append\n"))
        self.assertEqual(self.run_cli({"FOREMIND_SESSION": "c", "FOREMIND_ROLE": "controller"}, f)[0], 0)
        self.assertEqual(self.run_cli({}, f)[0], 0)
        self.assertEqual(self.read("capabilities.md"), "- x\n- x\n- x\n")
        self.assertEqual([e["by"] for e in self.events("catalog_applied")], ["supervisor", "controller", "user"])
