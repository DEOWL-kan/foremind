import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from foremind import batchlog, config, handoff, header, heartbeat, hooks, inbox, schemas, seat, telemetry, worktree
from foremind.events import EventLog
from foremind.hooks import guard

REPO = Path(__file__).resolve().parent.parent
SAMPLES = REPO / "tests" / "fixtures" / "hooks"  # real Claude Code hook inputs (M1-0), home as ~
SESSION, BATCH = "fm-shop-auth_2-1", "auth.2"


def usage_line(mid, tokens, *, sidechain=False, out=10):
    return json.dumps({"type": "assistant", "isSidechain": sidechain, "message": {"id": mid, "usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 0, "cache_read_input_tokens": tokens - 2,
        "output_tokens": out}}})


class HookBase(unittest.TestCase):
    def setUp(self):
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))  # expected hook_error lines; events record them
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.root = self.tmp / "shop"
        self.state = self.root / ".foremind"
        (self.state / "batches").mkdir(parents=True)
        (self.state / "exemptions").mkdir()
        self.cfg_home = self.tmp / "cfg"
        self.cfg_home.mkdir()
        self.transcript = self.tmp / "transcript.jsonl"
        self.base_env = {"FOREMIND_WT_ROOT": str(self.tmp / "wt"), "FOREMIND_CONFIG_HOME": str(self.cfg_home)}
        self.seat_env = {**self.base_env, "FOREMIND_PROJECT": str(self.root), "FOREMIND_SESSION": SESSION,
                         "FOREMIND_BATCH": BATCH, "FOREMIND_ROLE": "seat"}
        self.use_env(self.seat_env)
        self.jobs = self.enterContext(mock.patch("foremind.job.start", return_value="job-1"))  # a new Q-n's push job
        self.wt = self.wt_of(BATCH)  # M1-4 layout: <wt root>/<project>/<batch>/<repo>
        (self.wt / "src").mkdir(parents=True)
        self.write_header()
        self.hold(BATCH)

    def wt_of(self, batch):
        return worktree.batch_dir(seat.project_slug(self.root, {}), batch) / "api"

    def handoff_section(self, goal, nxt="NEXT-STEP"):  # M1-4 format, written the only way that counts (SF-3)
        sec = copy.deepcopy(schemas.EXAMPLES["handoff_section"])
        sec.update(goal=goal, next=[nxt])
        sec["state"]["repos"] = {"api": sec["state"]["repos"]["api"]}
        handoff.write_section(self.root, BATCH, sec, author=SESSION)

    def hold(self, batch, session=SESSION):
        (self.state / "batches" / f"{batch}.lock").write_text(session + "\n")  # M1-4 lock.py format

    def write_header(self, batch=BATCH, **over):
        head = {"id": batch, "plan_id": "auth", "repos": ["api"], "owns_paths": ["api:src/*", "api:package.json"],
                "state": "running", "hard_block": [], **over}
        (self.state / "batches" / f"{batch}.md").write_text(header.render(
            head, f"\n# {batch}\n\n## 状态\n\n进行中 STATUS-MARK\n\n## 备注\n\nNOT-STATUS\n"))

    def use_env(self, env):
        keep = {k: v for k, v in os.environ.items() if not k.startswith("FOREMIND_")}
        self.enterContext(mock.patch.dict(os.environ, {**keep, **env}, clear=True))

    def user_config(self, text):
        (self.cfg_home / "config.toml").write_text(text)

    def data(self, name, cwd=None, **tool_input):
        d = json.loads((SAMPLES / f"{name}.json").read_text())
        d.update(cwd=str(cwd or self.wt), transcript_path=str(self.transcript))  # never the real ~/.claude
        if tool_input:
            d["tool_input"] = {**d["tool_input"], **tool_input}
        return d

    def write(self, path, name="PreToolUse-Write"):
        return self.run_hook("PreToolUse", self.data(name, file_path=str(path)))

    def bash(self, command):
        return self.run_hook("PreToolUse", self.data("PreToolUse-Bash", command=command))

    def tool(self, name):
        d = self.data("PreToolUse-Bash")
        d.update(tool_name=name, tool_input={"q": "x"})
        return self.run_hook("PreToolUse", d)

    def run_hook(self, event, data):
        out = hooks.main(event, json.dumps(data))
        return json.loads(out) if out else None

    def events(self, type):
        return [e for e in EventLog(self.state / "events.jsonl").iter() if e["type"] == type]

    def hb(self):
        return heartbeat.read(self.root, SESSION)

    def assert_denied(self, out, *fragments):
        self.assertIsNotNone(out, "expected a deny")
        hso = out["hookSpecificOutput"]
        self.assertEqual((hso["hookEventName"], hso["permissionDecision"]), ("PreToolUse", "deny"))
        for f in fragments:
            self.assertIn(f, hso["permissionDecisionReason"])
        return hso["permissionDecisionReason"]

    def exemption(self, qid, match, *, category=4, batch=BATCH, expires=timedelta(days=1)):
        ts = (datetime.now(timezone.utc) + expires).isoformat(timespec="seconds")
        (self.state / "exemptions" / f"{qid}.json").write_text(
            json.dumps({"batch": batch, "category": category, "match": match, "expires_at": ts}))


class PreToolUseTest(HookBase):
    def test_owned_write_opens_and_post_closes_tool(self):
        w = "toolu_01KpWKM72NxMiYEe6A7hUwaM"  # the Write samples' tool_use_id
        self.assertIsNone(self.write(self.wt / "src/a.py"))
        hb = self.hb()
        self.assertEqual((hb["tool_open"], hb["open_tools"], hb["event"]), (True, [w], "PreToolUse"))
        self.assertEqual((hb["batch"], hb["role"], hb["handoff_requested"]), (BATCH, "seat", False))
        # SF-4: parallel calls Pre(W), Pre(B), Post(B): W is still running
        b = self.data("PreToolUse-Bash")
        b["tool_use_id"] = "toolu_B"
        self.run_hook("PreToolUse", b)
        self.run_hook("PostToolUse", {**self.data("PostToolUse-Bash"), "tool_use_id": "toolu_B"})
        self.assertEqual((self.hb()["tool_open"], self.hb()["open_tools"]), (True, [w]))
        self.run_hook("PostToolUse", self.data("PostToolUse-Write", file_path=str(self.wt / "src/a.py")))
        self.assertFalse(self.hb()["tool_open"])
        # relative paths resolve against the input's cwd; Edit goes through the same checks
        self.assertIsNone(self.run_hook("PreToolUse", self.data("PreToolUse-Edit", file_path="src/b.py")))

    def test_out_of_owns_paths_denied(self):
        self.assert_denied(self.write(self.wt / "README.md"), "api:README.md", "owns_paths", "foremind decide --new",
                           "#8")
        self.assertEqual(len(self.events("hook_denied")), 1)
        self.assertEqual(self.events("pending_needed"), [])
        self.assertFalse(self.hb()["tool_open"])  # a denied call never gets a PostToolUse

    def test_lock_holder(self):
        self.hold(BATCH, "fm-shop-auth_2-2")  # handed over to a successor
        target = self.wt / "src/a.py"
        self.assert_denied(self.write(target), "fm-shop-auth_2-2", "handoff --accept")
        (self.state / "batches" / f"{BATCH}.lock").unlink()
        self.assert_denied(self.write(target), "（无）")
        (self.state / "batches" / f"{BATCH}.lock").mkdir()  # unreadable lock: nobody provably holds it (SF-10)
        self.assert_denied(self.write(target), "锁文件读不到")

    def test_batch_follows_the_held_lock(self):
        # after a continuation the session holds auth.3's lock while FOREMIND_BATCH still says auth.2
        self.write_header("auth.3")
        (self.state / "batches" / f"{BATCH}.lock").unlink()
        self.hold("auth.3")
        (self.wt_of("auth.3") / "src").mkdir(parents=True)
        self.assertIsNone(self.write(self.wt_of("auth.3") / "src/a.py"))
        self.assertEqual(self.hb()["batch"], "auth.3")
        self.assert_denied(self.write(self.wt / "src/a.py"), "不在本批 auth.3 的 worktree")

    def test_outside_the_worktree(self):
        (self.root / "src").mkdir()
        self.assert_denied(self.write(self.root / "src/a.py"), "worktree")
        self.assert_denied(self.write(self.wt_of("auth.3") / "x.py"), "worktree")
        self.assertIsNone(self.write(self.tmp / "scratch.md"))

    def test_symlink_out_of_the_worktree(self):  # SF-9: paths are realpath'd, not just normalized
        (self.root / "src").mkdir()
        (self.wt / "src" / "esc").symlink_to(self.root / "src")
        self.assert_denied(self.write(self.wt / "src/esc/a.py"), "不在本批 auth.2 的 worktree")

    def test_repo_checkout_outside_the_project(self):  # SF-9 (repo branch of locate), SF-2
        main = self.tmp / "api-main"
        (main / "src").mkdir(parents=True)
        (self.root / "foremind.toml").write_text(f'[[repos]]\nid = "api"\npath = "{main}"\n')
        self.assert_denied(self.write(main / "src/a.py"), "不在本批")
        self.assert_denied(self.write(main / "package.json"), "#4")
        self.assertEqual(self.events("pending_needed")[-1]["target"], [str(main / "package.json"), "api:package.json"])
        # a config that fails to load still keeps [[repos]]: the main checkout stays protected
        (self.root / "foremind.toml").write_text(f'[[repos]]\nid = "api"\npath = "{main}"\n[zz_unregistered]\nkey = 50\n')  # unregistered = AUTHZ: the project layer may not set it
        self.assert_denied(self.write(main / "src/a.py"), "不在本批")
        self.assertTrue(any("zz_unregistered" in e["error"] for e in self.events("hook_error")))

    def test_bad_task_layer_keeps_the_other_layers(self):  # SF-2
        self.user_config('[hard_block]\npatterns = [{category = 19, paths = ["api:src/danger/*"]}]\n')
        self.write_header(config={"repos": []})  # a task may not set repo conventions: the task layer fails
        self.assert_denied(self.write(self.wt / "src/danger/x.py"), "#19")
        self.assertTrue(any("layer task" in e["error"] for e in self.events("hook_error")))

    def test_unreadable_config_keeps_every_rule(self):  # MF-A
        (self.root / "foremind.toml").write_bytes(b"# \xff\n")  # non-UTF-8, say a GBK comment
        self.assert_denied(self.write(self.wt / "CLAUDE.md"), "#22")
        self.assert_denied(self.write(self.wt / "README.md"), "owns_paths")
        self.assert_denied(self.write(self.wt / "package.json"), "#4")
        (self.root / "foremind.toml").unlink()
        self.user_config('[hard_block]\npatterns = [{category = [18], paths = ["x"]}]\n')  # unhashable category
        self.assert_denied(self.write(self.wt / "CLAUDE.md"), "#22")

    def test_evaluate_failure_falls_back_to_built_in_rules(self):  # MF-A
        orig = guard.evaluate

        def odd(*a, cfg, **kw):
            if cfg:
                raise TypeError("odd config")
            return orig(*a, cfg=cfg, **kw)
        self.user_config("[review]\nmax_rounds = 3\n")
        with mock.patch.object(guard, "evaluate", side_effect=odd):
            self.assert_denied(self.write(self.wt / "package.json"), "#4")
        self.assertTrue(any("odd config" in e["error"] for e in self.events("hook_error")))

    def test_config_fallback_keeps_header_categories(self):  # MF-B
        self.user_config('[hard_block]\npatterns = [{category = 18, paths = ["api:src/auth/*"]}]\n')
        self.write_header(hard_block=[18], config={"repos": []})  # the task layer fails; its category stays on
        self.assert_denied(self.write(self.wt / "src/auth/t.py"), "#18")
        (self.root / "foremind.toml").write_text("[zz_unregistered]\nkey = 50\n")  # now every merge fails
        self.assert_denied(self.write(self.wt / "src/auth/t.py"), "#18")

    def test_config_fallback_keeps_user_patterns(self):  # MF-B
        self.user_config('[hard_block]\npatterns = [{category = 19, paths = ["api:src/danger/*"]},\n'
                         '  {category = 18, paths = ["api:src/auth/*"]}]\n')
        for extra in ("", "[zz_unregistered]\nkey = 1\n"):  # merged (patterns is UNION), then every merge fails
            (self.root / "foremind.toml").write_text("[hard_block]\npatterns = []\ncategories = [18]\n" + extra)
            self.assert_denied(self.write(self.wt / "src/danger/x.py"), "#19")
            self.assert_denied(self.write(self.wt / "src/auth/t.py"), "#18")  # categories of every file, unioned
        self.assertTrue(self.events("hook_error"))

    def test_unreadable_header_keeps_batch_dir_and_exemption_limits(self):  # SF-A
        self.exemption("Q-3", {"paths": ["api:package.json"]})
        self.assertIsNone(self.write(self.wt / "package.json"))
        (self.state / "batches" / f"{BATCH}.md").unlink()  # the batch may be delivered by now
        self.assert_denied(self.write(self.wt / "package.json"), "#4")
        (self.wt.parent / "_ro" / "up" / "api").mkdir(parents=True)
        self.assert_denied(self.write(self.wt.parent / "_ro/up/api/x.py"), "不在本批")
        self.assert_denied(self.write(self.wt.parent / f"{SESSION}.settings.json"), "不在本批")

    def test_unreadable_header_keeps_lock_and_worktree_checks(self):  # SF-1
        (self.state / "batches" / f"{BATCH}.md").unlink()
        self.assertIsNone(self.write(self.wt / "README.md"))  # only owns_paths goes unchecked
        (self.root / "src").mkdir()
        self.assert_denied(self.write(self.root / "src/a.py"), "不在本批")
        self.hold(BATCH, "fm-shop-auth_2-2")
        self.assert_denied(self.write(self.wt / "README.md"), "锁持有者是 fm-shop-auth_2-2")
        self.assertEqual(len([e for e in self.events("hook_error") if "auth.2.md" in e["error"]]), 1)  # deduped

    def test_bad_environment_degrades(self):  # SF-3
        (self.state / "batches" / f"{BATCH}.lock").unlink()
        self.use_env({**self.seat_env, "FOREMIND_BATCH": "../etc"})
        self.assert_denied(self.write(self.wt / "CLAUDE.md"), "#22")
        self.assert_denied(self.write(self.wt / "src/a.py"), "没有批次")
        self.assertTrue(any("FOREMIND_BATCH" in e["error"] for e in self.events("hook_error")))
        self.hold(BATCH)
        self.use_env({**self.seat_env, "FOREMIND_SESSION": "fm-我的商城-auth.2-1"})  # heartbeat cannot be written
        self.assert_denied(self.write(self.wt / "CLAUDE.md"), "#22")
        self.assert_denied(self.write(self.wt / "src/a.py"), f"锁持有者是 {SESSION}")

    def test_bookkeeping_failures_never_undo_a_deny(self):  # MF-1
        (self.state / "heartbeats").write_text("not a directory")  # every heartbeat update now raises
        self.assert_denied(self.write(self.wt / "CLAUDE.md"), "#22")
        self.assert_denied(self.write(self.wt / "README.md"), "owns_paths")
        self.assertTrue(self.events("hook_error"))
        (self.state / "heartbeats").unlink()
        (self.state / "events.jsonl").write_text("{bad\n")  # every event append now raises
        self.assert_denied(self.write(self.wt / "CLAUDE.md"), "#22")
        self.assert_denied(self.write(self.wt / "README.md"), "owns_paths")
        self.assert_denied(self.bash("npm publish"), "#20")

    def test_system_config_is_a_hard_block(self):
        for p in (self.wt / "CLAUDE.md", self.wt / "sub" / "agents.md", self.wt / ".claude" / "settings.local.json",
                  self.root / "foremind.toml", self.state / "config.toml", self.state / "roles" / "seat.md",
                  self.state / "sessions" / f"{SESSION}.settings.json", self.cfg_home / "config.toml"):
            self.assert_denied(self.run_hook("PreToolUse", self.data("PreToolUse-Edit", file_path=str(p))), "#22")
        self.assertEqual({e["category"] for e in self.events("pending_needed")}, {22})

    def test_foremind_state_is_matrix_denied(self):  # SF-7
        self.exemption("Q-8", {"paths": ["*"]}, category=22)
        reason = self.assert_denied(self.write(self.state / "exemptions" / "Q-9.json"), "程序状态", "decide --new")
        self.assertNotIn("#22", reason)
        self.assert_denied(self.write(self.state / "batches" / f"{BATCH}.log.md"), "`foremind log`")
        self.assertEqual(self.events("pending_needed"), [])
        self.assertEqual(self.events("exemption_used"), [])
        self.use_env(self.base_env)  # the user's own session in the project: denied too
        self.assert_denied(self.run_hook("PreToolUse", self.data(
            "PreToolUse-Write", self.root, file_path=".foremind/events.jsonl")), "程序状态")

    def test_hard_block_and_exemptions(self):
        target = self.wt / "package.json"  # inside owns_paths, but a dependency manifest (#4)
        self.assert_denied(self.write(target), "#4", "*/package.json", "待决")
        [ev] = self.events("pending_needed")
        self.assertEqual((ev["category"], ev["kind"], ev["batch"], ev["session"]), (4, "paths", BATCH, SESSION))
        self.assertEqual(ev["target"], [str(target), "api:package.json"])
        self.assertEqual(len(ev["key"]), 16)

        self.exemption("Q-2", {"paths": ["api:package.json"]}, batch="auth.9")  # another batch's
        self.exemption("Q-4", {"paths": ["api:package.json"]}, expires=timedelta(seconds=-1))  # expired
        self.exemption("Q-5", {"commands": ["api:package.json"]})  # wrong kind
        self.exemption("Q-6", {"paths": ["api:*"]}, category=3)  # SF-8: another category's, however wide
        self.assert_denied(self.write(target), "#4")
        self.assert_denied(self.write(self.wt / "CLAUDE.md"), "#22")

        self.exemption("Q-3", {"paths": ["api:package.json"]})
        self.assertIsNone(self.write(target))
        [used] = self.events("exemption_used")
        self.assertEqual((used["exemption"], used["category"]), ("Q-3", 4))

        self.write_header(state="delivered")  # valid only until the batch is delivered
        self.assert_denied(self.write(target), "#4")

    def test_bash_command_patterns(self):
        self.assertIsNone(self.run_hook("PreToolUse", self.data("PreToolUse-Bash")))  # echo done
        cmd = "cd web && FOO=1 sudo npm  install\tleft-pad | tee log"  # N-3: whitespace runs are one space
        raw = "FOO=1 sudo npm install left-pad"  # m2a.5.F1: what a request and an exemption name, prefixes kept
        self.assert_denied(self.bash(cmd), raw)
        [ev] = self.events("pending_needed")
        self.assertEqual((ev["kind"], ev["target"], ev["pattern"]), ("commands", [raw], "npm install *"))
        # Bash is never path-checked: writing a system file through the shell is left to gate/L0
        self.assertIsNone(self.bash("echo x > CLAUDE.md"))
        self.exemption("Q-6", {"commands": ["npm install left-*"]})
        self.assertIsNone(self.bash("cd web && npm  install left-pad | tee log"))
        self.assert_denied(self.bash(cmd), raw)  # VAR=value and sudo change what runs: not covered
        for cmd2 in ("FOO=1 bash <<'EOF'\nnpm install left-pad\nEOF", "cat <<'EOF' | sudo sh\nnpm install left-pad\nEOF"):
            self.assert_denied(self.bash(cmd2), "#4")  # m2b.2 r1: a heredoc body a shell reads keeps its reader
        self.exemption("Q-7", {"commands": ["FOO=1 sudo npm install left-*"]})
        self.assertIsNone(self.bash(cmd))
        # an exemption for one segment does not cover another hit in the same command
        self.assert_denied(self.bash("npm install left-pad; npm publish"), "npm publish", "#20")

    def test_mcp_tools_blocked_by_default(self):  # MF-2
        self.assert_denied(self.tool("mcp__docs__read"), "mcp__docs__read", "#20", "mcp__*")
        (self.root / "foremind.toml").write_text('[hard_block]\nallow_tools = ["mcp__docs__*"]\n')
        self.assert_denied(self.tool("mcp__docs__read"), "#20")  # only the user layer may allow a tool
        self.user_config('[hard_block]\nallow_tools = ["mcp__docs__*"]\n')
        self.assertIsNone(self.tool("mcp__docs__read"))
        self.assert_denied(self.tool("mcp__gmail__send"), "#20")
        self.exemption("Q-7", {"tools": ["mcp__gmail__*"]}, category=20)
        self.assertIsNone(self.tool("mcp__gmail__send"))
        self.assertIsNone(self.tool("Read"))
        self.use_env(self.base_env)  # not a Foremind session: tools are not its business
        d = self.data("PreToolUse-Bash", self.root)
        d.update(tool_name="mcp__slack__post", tool_input={})
        self.assertIsNone(self.run_hook("PreToolUse", d))

    def test_configured_patterns_and_categories(self):
        self.user_config('[hard_block]\nallow_tools = ["mcp__gmail__*"]\n'
                         'patterns = [{category = 18, tools = ["mcp__gmail__*"]},\n'
                         '  {category = 18, paths = ["api:src/auth/*"]}]\n')
        auth = self.wt / "src/auth/token.py"
        self.assertIsNone(self.write(auth))  # 18 not on
        self.assertIsNone(self.tool("mcp__gmail__send"))
        self.write_header(hard_block=[18])  # the batch header turns category 18 on (§20 I10)
        self.assert_denied(self.write(auth), "#18")
        self.assert_denied(self.tool("mcp__gmail__send"), "#18")  # allow_tools lifts only the #20 default

    def test_planner_writes_no_product_code(self):
        self.use_env({**self.base_env, "FOREMIND_PROJECT": str(self.root), "FOREMIND_SESSION": "fm-shop-planner-1",
                      "FOREMIND_ROLE": "planner"})
        (self.root / "src").mkdir()
        self.assert_denied(self.write(self.root / "src/a.py"), "planner")
        self.assert_denied(self.write(self.wt / "src/a.py"), "planner")
        self.assertIsNone(self.write(self.tmp / "draft.md"))

    def test_non_foremind_session_gets_only_the_system_guard(self):
        self.use_env(self.base_env)
        (self.root / "src").mkdir()
        self.assertIsNone(self.run_hook("PreToolUse", self.data("PreToolUse-Write", self.root, file_path="src/a.py")))
        self.assertIsNone(self.run_hook("PreToolUse", self.data("PreToolUse-Write", self.root, file_path="package.json")))
        self.assertIsNone(self.run_hook("PreToolUse", self.data("PreToolUse-Bash", self.root, command="npm install x")))
        self.assert_denied(self.run_hook("PreToolUse", self.data(
            "PreToolUse-Edit", self.root, file_path=".foremind/delivery.toml")), "程序独占", "foremind doctor --rescan")
        self.assertEqual(len(self.events("hook_denied")), 1)
        self.assertEqual(self.events("pending_needed"), [])
        for event in ("SessionStart", "PostToolUse", "Stop"):
            self.assertIsNone(self.run_hook(event, self.data(
                {"SessionStart": "SessionStart", "PostToolUse": "PostToolUse-Write", "Stop": "Stop"}[event], self.root)))
        self.assertFalse((self.state / "heartbeats").exists())

    def test_user_session_edits_system_config(self):  # I51
        claude_md = self.root / "CLAUDE.md"
        self.assert_denied(self.run_hook("PreToolUse", self.data("PreToolUse-Edit", self.root, file_path="CLAUDE.md")),
                           "#22")  # a Foremind seat: still blocked
        (self.state / "events.jsonl").unlink()  # the seat's denial is logged; start over for the user's session
        self.use_env(self.base_env)
        for name in ("Edit", "Write"):
            self.assertIsNone(self.run_hook("PreToolUse", self.data(f"PreToolUse-{name}", self.root,
                                                                    file_path="CLAUDE.md")))
            claude_md.write_text(f"by {name}\n")
            self.run_hook("PostToolUse", self.data(f"PostToolUse-{name}", self.root, file_path="CLAUDE.md"))
        self.assertIsNone(self.run_hook("PreToolUse", self.data("PreToolUse-Edit", self.root,
                                                                file_path=".foremind/config.toml")))
        self.run_hook("PostToolUse", self.data("PostToolUse-Write", self.root, file_path=".foremind/config.toml"))
        self.run_hook("PostToolUse", self.data("PostToolUse-Write", self.root, file_path="src/a.py"))  # not #22
        self.run_hook("PostToolUse", self.data("PostToolUse-Bash", self.root))
        evs = self.events("user_config_edit")
        sid = json.loads((SAMPLES / "PostToolUse-Edit.json").read_text())["session_id"]
        self.assertEqual([(e["session"], e["path"]) for e in evs],
                         [(sid, str(claude_md))] * 2 + [(sid, str(self.state / "config.toml"))])
        self.assertEqual(evs[1]["sha256"], hashlib.sha256(b"by Write\n").hexdigest())
        self.assertIsNone(evs[2]["sha256"])  # never written: unreadable
        self.assertEqual((self.events("hook_denied"), self.events("pending_needed")), ([], []))
        (self.state / "events.jsonl").write_text("{bad\n")  # bookkeeping fails: still no output, no raise
        self.assertIsNone(self.run_hook("PostToolUse", self.data("PostToolUse-Edit", self.root, file_path="CLAUDE.md")))

    def test_outside_every_project_does_nothing(self):  # SF-6
        self.use_env(self.base_env)
        elsewhere = self.tmp / "other-repo"
        elsewhere.mkdir()
        for p in ("CLAUDE.md", "AGENTS.md", ".claude/settings.json"):
            self.assertIsNone(self.run_hook("PreToolUse", self.data("PreToolUse-Edit", elsewhere, file_path=p)))
            self.assertIsNone(self.run_hook("PostToolUse", self.data("PostToolUse-Edit", elsewhere, file_path=p)))
        self.assertIsNone(self.run_hook("UserPromptSubmit", self.data("UserPromptSubmit", elsewhere)))
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertEqual(self.events("hook_error"), [])

    def test_internal_errors_never_block(self):
        target = self.wt / "README.md"  # would be denied by owns_paths
        with mock.patch.object(guard, "evaluate", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.write(target))
        self.assertIn("boom", self.events("hook_error")[-1]["error"])
        # broken config: built-in rules only
        self.user_config("not = [valid")
        self.assert_denied(self.write(self.wt / "package.json"), "#4")
        self.assertIsNone(hooks.main("PreToolUse", "not json"))
        self.assertIsNone(hooks.main("PreToolUse", "[1, 2]"))
        self.assertIsNone(hooks.main("NoSuchEvent", "{}"))
        self.assertGreaterEqual(len(self.events("hook_error")), 4)

    def test_a_call_after_stop_in_the_transcript_is_a_real_turn(self):  # REQ-15
        def tool_use(tid, **kw):
            return json.dumps({"type": "assistant", **kw, "message": {"content": [
                {"type": "text", "text": "go on"}, {"type": "tool_use", "id": tid, "name": "Write", "input": {}}]}})

        self.run_hook("Stop", {**self.data("Stop"), "stop_hook_active": False})
        self.transcript.write_text("\n".join([
            tool_use("toolu_real"), tool_use("toolu_side", isSidechain=True),
            json.dumps({"type": "user", "message": {"content": "toolu_said is only mentioned"}})]) + "\n")
        for tid in ("toolu_side", "toolu_said", "toolu_ghost"):
            self.run_hook("PreToolUse", {**self.data("PreToolUse-Write", file_path=str(self.wt / "src/a.py")),
                                         "tool_use_id": tid})
        self.assertEqual((self.hb()["tool_open"], self.hb()["event"]), (False, "Stop"))
        self.assertEqual([e["tool_use_id"] for e in self.events("tool_after_stop")],
                         ["toolu_side", "toolu_said", "toolu_ghost"])
        self.run_hook("PreToolUse", {**self.data("PreToolUse-Write", file_path=str(self.wt / "src/a.py")),
                                     "tool_use_id": "toolu_real"})
        self.assertEqual(self.hb()["open_tools"], ["toolu_real"])
        self.assertEqual(len(self.events("tool_after_stop")), 3)
        self.assertFalse(telemetry.has_tool_use(str(self.transcript), "toolu_real", tail=10))  # beyond the tail

    def test_a_late_post_tool_use_confirms_a_tool_after_stop(self):  # REQ-12, m2c.7.F2
        self.run_hook("Stop", {**self.data("Stop"), "stop_hook_active": False})
        for tid in ("toolu_late", "toolu_other"):
            self.run_hook("PreToolUse", {**self.data("PreToolUse-Write", file_path=str(self.wt / "src/a.py")),
                                         "tool_use_id": tid})
        self.run_hook("PostToolUse", {**self.data("PostToolUse-Write"), "tool_use_id": "toolu_late"})
        self.run_hook("PostToolUse", {**self.data("PostToolUse-Write"), "tool_use_id": "toolu_other"})  # not Stop now
        self.run_hook("PostToolUse", {**self.data("PostToolUse-Write"), "tool_use_id": "toolu_never"})
        es = self.events("tool_after_stop_confirmed")
        self.assertEqual([(e["session"], e["batch"], e["tool_use_id"]) for e in es],
                         [(SESSION, BATCH, "toolu_late"), (SESSION, BATCH, "toolu_other")])


    def test_parallel_calls_after_stop_all_get_confirmed(self):  # REQ-12: a turn's first calls come in parallel
        from concurrent.futures import ThreadPoolExecutor
        self.run_hook("Stop", {**self.data("Stop"), "stop_hook_active": False})
        real = hooks._after_stop_ids
        slow = lambda ctx: (time.sleep(0.05), real(ctx))[1]  # noqa: E731 — widens the read-then-write window
        tids = [f"toolu_p{n}" for n in range(6)]
        with mock.patch.object(hooks, "_after_stop_ids", slow), ThreadPoolExecutor(len(tids)) as pool:
            list(pool.map(lambda t: self.run_hook("PreToolUse", {
                **self.data("PreToolUse-Write", file_path=str(self.wt / "src/a.py")), "tool_use_id": t}), tids))
        for t in tids:
            self.run_hook("PostToolUse", {**self.data("PostToolUse-Write"), "tool_use_id": t})
        self.assertEqual(sorted(e["tool_use_id"] for e in self.events("tool_after_stop_confirmed")), tids)

class PostToolUseTest(HookBase):
    def test_refreshes_context_from_the_transcript_tail(self):  # SF-9, N-1
        self.transcript.write_text("\n".join([usage_line("m1", 5000), usage_line("m2", 9000),
                                              usage_line("s1", 777_777, sidechain=True)]) + "\n")
        self.run_hook("PostToolUse", self.data("PostToolUse-Write", file_path=str(self.wt / "src/a.py")))
        p = self.state / "telemetry" / f"{SESSION}.context.json"
        self.assertEqual(json.loads(p.read_text())["context_tokens"], 9000)
        d = self.data("Stop")
        d["stop_hook_active"] = True
        self.run_hook("Stop", d)  # Stop writes the full statistics ...
        self.transcript.write_text(self.transcript.read_text() + usage_line("m3", 9500) + "\n")
        self.run_hook("PostToolUse", self.data("PostToolUse-Write", file_path=str(self.wt / "src/a.py")))
        snap = json.loads(p.read_text())  # ... which PostToolUse keeps while refreshing the size
        self.assertEqual((snap["context_tokens"], snap["requests"]), (9500, 2))


class SessionStartTest(HookBase):
    def test_injects_l1_and_binds_session(self):
        (self.state / "batches" / f"{BATCH}.handoff.md").write_text("# auth.2 登录令牌\nHANDOFF-DOC-MARK\n")
        records = "\n".join(f"- {BATCH}.D{n} · 选了方案{n} · 没选别的 · 理由{n} · #1 · x.py" for n in range(1, 12))
        batchlog.append(self.root, BATCH, records, author=SESSION)
        batchlog.append(self.root, BATCH, f"{BATCH}.F1 · 改时区 → 仍失败 → 缓存 · log:3", author=SESSION)
        for goal in ("OLD-GOAL", "HANDOFF-GOAL"):  # the latest one wins
            self.handoff_section(goal)

        out = self.run_hook("SessionStart", self.data("SessionStart"))
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SessionStart")
        ctx = hso["additionalContext"]
        for s in (SESSION, "HANDOFF-DOC-MARK", "STATUS-MARK", "HANDOFF-GOAL", "NEXT-STEP",
                  f"- {BATCH}.D11 · 选了方案11", f"{BATCH}.F1 · 改时区", f"{BATCH}.D1 · 选了方案1\n"):
            self.assertIn(s, ctx)
        self.assertNotIn("NOT-STATUS", ctx)
        self.assertNotIn("OLD-GOAL", ctx)
        self.assertNotIn("理由1 ", ctx)  # D1 and D2 are older than the last 10: id + gist only
        self.assertIn("理由3", ctx)

        sid = "7e068738-6aef-451c-88b8-d9fc42ff3c5e"
        self.assertEqual((self.hb()["agent_session_id"], self.hb()["event"]), (sid, "SessionStart"))
        [bound] = self.events("session_bound")
        self.assertEqual((bound["session"], bound["agent_session_id"], bound["source"]), (SESSION, sid, "startup"))
        self.run_hook("SessionStart", self.data("SessionStart"))  # same agent session: no rebinding
        self.assertEqual(len(self.events("session_bound")), 1)
        resumed = self.data("SessionStart")
        resumed.update(session_id="11111111-2222-3333-4444-555555555555", source="resume")
        self.run_hook("SessionStart", resumed)
        self.assertEqual([e["source"] for e in self.events("session_bound")], ["startup", "resume"])

    def test_records_the_messaging_socket(self):  # REQ-12
        self.use_env({**self.seat_env, "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc.sock"})
        self.run_hook("SessionStart", self.data("SessionStart"))
        self.assertEqual(self.hb()["messaging_socket"], "/tmp/cc.sock")

    def l1(self):
        return self.run_hook("SessionStart", self.data("SessionStart"))["hookSpecificOutput"]["additionalContext"]

    def test_short_note_when_l1_unreadable(self):
        (self.state / "batches" / f"{BATCH}.md").unlink()
        self.assertIn("未能读取本批考纲", self.l1())
        self.write_header()  # header and status only: say the handoff doc is missing, give the header points
        ctx = self.l1()
        for s in ("未能读取交接文档", "## 批次头要点", '- owns_paths: ["api:src/*", "api:package.json"]',
                  "- hard_block: []", "STATUS-MARK"):
            self.assertIn(s, ctx)
        self.assertNotIn("## 目标", ctx)  # no goal.md for plan auth
        (self.state / "batches" / f"{BATCH}.handoff.md").write_bytes(b"\xff\xfe not utf-8")  # SF-10
        self.assertIn("## 批次头要点", self.l1())

    def test_l1_falls_back_to_header_points_and_goal(self):  # finding 2
        self.write_header(mode="auto", start_commands=["make test"], state="running", accept_commands=["make check"],
                          must_read=[{"path": "api:src/a.py:1-9", "why": "MUST-READ"}],
                          tools=[{"name": "rg", "step": "TOOL-STEP"}],
                          tiers={"difficulty": "M", "effort": "high", "model": "NOT-SHOWN", "reason": "NOT-SHOWN"})
        goal = self.state / "plans" / "auth" / "goal.md"
        goal.parent.mkdir(parents=True)
        goal.write_text("---\nsha256: FROZEN-HASH\n---\n# 目标\n\nGOAL-MARK REQ-1\n")
        ctx = self.l1()
        for s in ("## 批次头要点", "- mode: auto", '- start_commands: ["make test"]',
                  '- tiers: {"difficulty": "M", "effort": "high"}', '- accept_commands: ["make check"]',
                  '- must_read: [{"path": "api:src/a.py:1-9", "why": "MUST-READ"}]',
                  '- tools: [{"name": "rg", "step": "TOOL-STEP"}]', "## 目标", "GOAL-MARK REQ-1", "STATUS-MARK"):
            self.assertIn(s, ctx)
        for s in ("NOT-SHOWN", "FROZEN-HASH", "- state:"):
            self.assertNotIn(s, ctx)
        goal.write_text("# 目标\nGOAL-HEAD\n" + "长" * 20_000)  # too long: the goal is cut, the rest stays whole
        ctx = self.l1()
        self.assertLessEqual(len(ctx.encode()), hooks.L1_MAX_BYTES)
        for s in ("GOAL-HEAD", "目标超过 L1 上限", ".foremind/plans/auth/goal.md", "- mode: auto", "STATUS-MARK"):
            self.assertIn(s, ctx)

    def test_l1_status_skips_a_fenced_status_line(self):  # m2b.9 r1/r2 note: cut like plan.model.spec
        (self.state / "batches" / f"{BATCH}.md").write_text(header.render(
            {"id": BATCH, "plan_id": "auth", "repos": ["api"], "owns_paths": ["api:src/*"], "state": "running",
             "hard_block": []},
            f"\n# {BATCH}\n\n```\n## 状态\nFENCED-MARK\n```\n\n## 状态\n\nREAL-MARK\n\n## 备注\n\nNOT-STATUS\n"))
        status = self.l1().split("## 状态区", 1)[1].split("\n## ", 1)[0]
        self.assertIn("REAL-MARK", status)
        self.assertNotIn("FENCED-MARK", status)
        self.assertNotIn("NOT-STATUS", status)

    def test_l1_is_capped(self):  # N-10
        (self.state / "batches" / f"{BATCH}.handoff.md").write_text("HEAD-MARK\n" + "长" * 20_000)
        ctx = self.l1()
        self.assertLessEqual(len(ctx.encode()), hooks.L1_MAX_BYTES)  # N-a: bytes, not characters
        for s in ("HEAD-MARK", "交接文档超过", "STATUS-MARK"):  # only the handoff doc is cut
            self.assertIn(s, ctx)

    def test_rewritten_log_is_said_not_skipped(self):  # SF-B
        self.handoff_section("GOAL-G")
        log = self.state / "batches" / f"{BATCH}.log.md"
        log.write_text(log.read_text().replace("GOAL-G", "GOAL-X"))
        ctx = self.l1()
        self.assertIn("未通过校验，交接段未注入", ctx)
        self.assertNotIn('"goal"', ctx)
        self.assertTrue(any("LogRewritten" in e["error"] for e in self.events("hook_error")))

    def test_df_index_only_from_the_recorded_length(self):  # m2b.8 item 5
        batchlog.append(self.root, BATCH, f"- {BATCH}.D1 · a · b · c · #1 · p", author=SESSION)
        with open(self.state / "batches" / f"{BATCH}.log.md", "a") as f:  # verify() still passes: a longer file
            f.write(f"- {BATCH}.D2 · UNRECORDED · b · c · #1 · p\n")
        ctx = self.l1()
        self.assertIn(f"{BATCH}.D1", ctx)
        self.assertNotIn("UNRECORDED", ctx)

    def test_session_without_batch_gets_heartbeat_only(self):
        self.use_env({**self.base_env, "FOREMIND_PROJECT": str(self.root), "FOREMIND_SESSION": "fm-shop-controller-1",
                      "FOREMIND_ROLE": "controller"})
        self.assertIsNone(self.run_hook("SessionStart", self.data("SessionStart")))
        self.assertEqual(heartbeat.read(self.root, "fm-shop-controller-1")["role"], "controller")


class StopTest(HookBase):
    def usage(self, tokens):
        self.transcript.write_text(usage_line("msg_1", tokens) + "\n")

    def stop(self, active=False, emit=None):
        d = self.data("Stop")
        d["stop_hook_active"] = active
        out = hooks.main("Stop", json.dumps(d), emit=emit)
        return json.loads(out) if out else None

    def test_hard_threshold_blocks_but_never_loops(self):
        self.usage(170_000)  # defaults: effective = min(200k × 80%, 180k) = 160k
        out = self.stop()
        self.assertEqual(out["decision"], "block")
        self.assertIn("硬阈值", out["reason"])
        self.assertNotIn("早于本次请求", out["reason"])
        self.assertTrue(self.hb()["handoff_requested"])
        self.assertEqual(self.hb()["transcript_path"], str(self.transcript))  # for stuck.py (m2c.2)
        self.assertIsNone(self.stop(active=True))  # the continued turn may stop
        self.assertEqual(self.stop()["decision"], "block")  # a later turn is blocked again
        snap = json.loads((self.state / "telemetry" / f"{SESSION}.context.json").read_text())
        self.assertEqual(snap["context_tokens"], 170_000)

    def test_a_section_written_before_the_request_is_asked_again(self):  # finding 32
        self.handoff_section("written just before the request")
        at = self.events("handoff_written")[0]["ts"]
        self.usage(170_000)
        reason = self.stop()["reason"]
        self.assertIn(f"你在 {at} 写的交接段早于本次请求，不作数；现在用 `foremind handoff --write` 再写一次", reason)
        self.assertIsNone(self.stop(active=True))
        self.assertNotIn("早于本次请求", self.stop()["reason"], "only the first request says it")

    def test_bookkeeping_failures_never_undo_a_block(self):  # MF-1
        self.usage(170_000)
        with mock.patch.object(inbox, "pending_messages", side_effect=inbox.InboxCorrupt("torn")):
            self.assertEqual(self.stop()["decision"], "block")
        (self.state / "events.jsonl").write_text("{bad\n")
        for d in ("heartbeats", "telemetry"):
            shutil.rmtree(self.state / d)
            (self.state / d).write_text("not a directory")
        self.assertEqual(self.stop()["decision"], "block")

    def test_soft_threshold_prompts_once(self):
        self.usage(100_000)
        self.assertIsNone(self.stop())
        self.usage(140_000)  # soft = 160k × 65 / 80 = 130k
        out = self.stop()
        self.assertIn("软阈值", out["reason"])
        self.assertFalse(self.hb()["handoff_requested"])
        self.assertIsNone(self.stop())

    def test_thresholds_from_config(self):
        self.user_config("[context]\nabs_cap_tokens = 50000\n")
        self.usage(60_000)
        self.assertIn("硬阈值", self.stop()["reason"])

    def test_soft_threshold_under_the_absolute_cap(self):  # SF-9: a 1M window is capped at 180k, soft scales down
        self.assertEqual(hooks.budget({"context.window_tokens": 1_000_000}, "seat", None), (146_250, 180_000))
        self.user_config("[context]\nwindow_tokens = 1000000\n")
        self.usage(150_000)
        self.assertIn("软阈值 146250", self.stop()["reason"])

    def test_budget_by_role_and_model(self):  # m2b.8 item 4
        cfg = {"context.hard_pct": 50, "context.abs_cap_tokens": 90_000, "context.by_role": {
            "seat": {"hard_pct": 60, "claude-opus-5-5": {"window_tokens": 100_000, "soft_pct": 30}},
            "controller": {"claude-opus-5-5": "not a table"}}}
        self.assertEqual(hooks.budget(cfg, "seat", "claude-opus-5-5"), (30_000, 60_000))  # model, role, then flat
        self.assertEqual(hooks.budget(cfg, "seat", "other")[1], 90_000)  # role's 60% of 200k, under the flat cap
        self.assertEqual(hooks.budget(cfg, "controller", "claude-opus-5-5")[1], 90_000)  # 50% of 200k = 100k, capped
        self.assertEqual(hooks.budget(cfg, None, None), hooks.budget(cfg, "planner", "claude-opus-5-5"))

    def test_budget_by_role_merges_key_by_key_across_layers(self):  # m2b.8 r1-r3 note: controller registration
        self.user_config('[context.by_role.seat."claude-opus-5-5"]\nabs_cap_tokens = 50000\n')
        (self.root / ".foremind" / "config.toml").write_text('[context.by_role.seat]\nhard_pct = 60\n')
        cfg = config.load(self.root)
        self.assertEqual(cfg["context.by_role.seat.claude-opus-5-5.abs_cap_tokens"], 50000)  # not replaced
        self.assertEqual(cfg["context.by_role.seat.hard_pct"], 60)
        self.assertEqual(hooks.budget(cfg, "seat", "claude-opus-5-5")[1], 50_000)  # the user's model cap survives
        self.assertEqual(hooks.budget(cfg, "seat", "other")[1], 120_000)  # the project's role hard_pct: 60% of 200k

    def test_model_from_the_status_line_else_the_launch(self):
        self.user_config('[context.by_role.seat."claude-opus-5-5"]\nabs_cap_tokens = 50000\n')
        self.usage(60_000)
        self.assertIsNone(self.stop(), "model unknown: the flat keys (160k)")
        EventLog(self.state / "events.jsonl").append("seat_launch", phase="intent", dedupe_id="seat_launch:x",
                                                     session=SESSION, model="claude-opus-5-5")
        self.assertIn("硬阈值（有效预算 50000）", self.stop()["reason"])
        tel = self.state / "telemetry" / f"{SESSION}.statusline.json"
        tel.write_text(json.dumps({"model": {"id": "claude-fable-5-1", "display_name": "Fable"}}))
        self.assertEqual(hooks._model(self.root, SESSION), "claude-fable-5-1")  # what runs now wins
        tel.write_text(json.dumps({"model": {"id": "claude-opus-5-5[1m]"}}))  # spelled otherwise: no entry
        self.assertIn("硬阈值（有效预算 50000）", self.stop()["reason"])  # the launch model's entry

    def test_inbox_delivery(self):
        inbox.append(SESSION, "msg A", sender="controller", root=self.root)
        inbox.append(SESSION, "msg B", sender="user", root=self.root)
        self.assertIsNone(self.stop(active=True))  # cannot block, so nothing is taken from the inbox
        self.assertEqual(len(inbox.pending_messages(SESSION, root=self.root)), 2)
        seen = []  # still pending while the output is written: the cursor moves only after that
        out = self.stop(emit=lambda text: seen.append(len(inbox.pending_messages(SESSION, root=self.root))))
        self.assertEqual(seen, [2])
        self.assertEqual(out["decision"], "block")
        self.assertIn("［controller］msg A", out["reason"])
        self.assertIn("［user］msg B", out["reason"])
        self.assertEqual(inbox.pending_messages(SESSION, root=self.root), [])
        inbox.append(SESSION, "msg C", sender="user", root=self.root)

        def broken_pipe(text):
            raise BrokenPipeError

        self.assertIsNone(self.stop(emit=broken_pipe))
        self.assertEqual([m.text for m in inbox.pending_messages(SESSION, root=self.root)], ["msg C"])

    def test_clears_open_tool(self):
        self.write(self.wt / "src/a.py")
        self.assertTrue(self.hb()["tool_open"])
        self.stop()
        self.assertEqual((self.hb()["tool_open"], self.hb()["open_tools"]), (False, []))


class StopFailureTest(HookBase):  # REQ-11
    def test_records_the_error_type(self):
        d = {**self.data("Stop"), "hook_event_name": "StopFailure", "error": "rate_limit"}
        self.assertIsNone(self.run_hook("StopFailure", d))
        self.assertEqual(self.hb()["api_error"]["type"], "rate_limit")
        self.assertNotEqual(self.hb().get("event"), "StopFailure")  # the last turn hook stays for the supervisor
        del d["error"]
        self.run_hook("StopFailure", {**d, "error_type": "overloaded"})
        self.run_hook("StopFailure", d)
        self.assertEqual([(e["session"], e["batch"], e["error_type"]) for e in self.events("api_error")],
                         [(SESSION, BATCH, t) for t in ("rate_limit", "overloaded", "unknown")])
        self.assertIn("at", self.hb()["api_error"])

    def test_not_a_foremind_session(self):
        self.use_env(self.base_env)
        self.assertIsNone(self.run_hook("StopFailure", {**self.data("Stop"), "error": "rate_limit"}))
        self.assertEqual(self.events("api_error"), [])


class UserPromptSubmitTest(HookBase):
    def test_presence_skips_program_deliveries(self):
        self.use_env(self.base_env)  # the user's own session
        now = hooks.time.time()
        clock = self.enterContext(mock.patch.object(hooks.time, "time", return_value=now))
        d = self.data("UserPromptSubmit", self.root)
        d["prompt"] = "  第一行\r\n第二行\r第三行 \n"
        self.assertIsNone(self.run_hook("UserPromptSubmit", d))
        self.assertEqual(len(self.events("user_present")), 1)
        digest = hooks._sha("第一行\n第二行\n第三行")  # I42: what the carrier hashed
        EventLog(self.state / "events.jsonl").append("program_delivery", session=SESSION, text_sha256=digest)
        self.run_hook("UserPromptSubmit", d)
        self.assertEqual(len(self.events("user_present")), 1)
        with mock.patch.object(hooks, "DELIVERY_WINDOW", timedelta(seconds=-60)):  # delivery too old to match
            self.run_hook("UserPromptSubmit", d)
            self.assertEqual(len(self.events("user_present")), 1)  # finding 8: once per session and 10 minutes
            clock.return_value = now + 600
            self.run_hook("UserPromptSubmit", d)
            self.run_hook("UserPromptSubmit", {**d, "session_id": "another-agent-session"})
        found = self.events("user_present")
        self.assertEqual(len(found), 3)
        self.assertNotIn("session", found[0])
        self.assertEqual(found[2]["agent_session_id"], "another-agent-session")

    def test_foremind_sessions_are_never_presence(self):  # SF-5
        self.assertIsNone(self.run_hook("UserPromptSubmit", self.data("UserPromptSubmit")))
        self.assertEqual(self.events("user_present"), [])


class CommandTest(HookBase):
    def cli(self, args, stdin, **env):
        return subprocess.run([sys.executable, "-m", "foremind", *args], cwd=REPO, input=stdin.encode(),
                              capture_output=True, env={**os.environ, **env})

    def test_cli_deny_and_garbage(self):
        d = self.data("PreToolUse-Write", file_path=str(self.wt / "README.md"))
        r = self.cli(["hook", "PreToolUse"], json.dumps(d), PYTHONIOENCODING="ascii")  # SF-10: any stdout encoding
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        r = self.cli(["hook", "PreToolUse"], "not json")
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        r = self.cli(["hook"], json.dumps(d))  # N-14: a missing event is a no-op, not argparse's exit 2
        self.assertEqual((r.returncode, r.stdout), (0, b""))

    def test_every_sample_runs_clean(self):
        for f in sorted(SAMPLES.glob("*.json")):
            event = f.stem.split("-")[0]
            r = self.cli(["hook", event], json.dumps(self.data(f.stem)))
            self.assertEqual(r.returncode, 0, f"{f.name}: {r.stderr}")
        self.assertEqual(self.events("hook_error"), [])


if __name__ == "__main__":
    unittest.main()
