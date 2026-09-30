import contextlib
import json
import os
import shlex
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from foremind import gate, manifests
from foremind.decide.exemption import narrow
from foremind.hooks import guard
from foremind.vendors import claude

SESSION, BATCH = "fm-shop-b_1-1", "b.1"


def ev(cmd, **kw):
    kw = {"session": SESSION, "batch": BATCH, "header": {"repos": ["api"]}, "cfg": {}, **kw}
    return guard.evaluate("Bash", {"command": cmd}, "/", **kw)


def hits(cmd, **kw):
    return [(h.category, h.values[0]) for h in ev(cmd, **kw).pending]


def whole(cmd):  # the raw text a heredoc body's commands carry: the whole command, quotes gone, one space
    return " ".join(shlex.split(cmd))


class SegmentsTest(unittest.TestCase):
    def test_quoted_and_heredoc_text_is_not_a_command(self):  # m2a.2 run, 2026-09-26 09:11
        for cmd in ("foremind log m2a.2 <<'EOF'\n- note: `npm install left-*` was a false hit; don't\nEOF\necho done",
                    "foremind handoff --write b.1 <<EOF\nuse \\`npm install x\\` and \\$(npm install y)\nEOF",
                    'git commit -m "$(cat <<\'EOF\'\nfix: npm install left-pad; npm publish\nEOF\n)"',
                    "echo 'npm install q; npm publish'",
                    'foremind log b.1 "retried npm install x (twice)"',
                    "x=$(\n  cat <<'E'\n) npm install nope\nE\n)",
                    "cat <<-'EOF' > notes.md\n\tnpm install a\n\tEOF\necho ok",
                    "foremind log b.1 <<'EOF'\nbypass found: bash -c 'npm install x'\nEOF"):
            self.assertEqual(hits(cmd), [], cmd)

    def test_executed_text_still_hits(self):
        for cmd, seg in (('echo "a $(npm install z) b"', "npm install z"),
                         ("echo `npm install z`", "npm install z"),
                         ("cat <<EOF > f\n$(npm install y)\nEOF", "npm install y"),  # unquoted: substitution runs
                         # a shell reads the body; its commands carry the whole command (r1: prefixes, the reader)
                         ("bash <<'EOF'\nnpm install x\nEOF", "bash <<EOF npm install x EOF"),
                         ("cat <<'EOF' | sh\nnpm install x\nEOF", "cat <<EOF | sh npm install x EOF"),
                         ('bash -c "$(cat <<\'EOF\'\nnpm install x\nEOF\n)"', "bash -c $(cat <<'EOF'\nnpm install x\nEOF\n)"),
                         ("echo hi # don't\nnpm install v", "npm install v"),  # the quote is in a comment
                         ("echo $((1<<2))\nnpm install w\n2", "npm install w"),  # arithmetic, no heredoc
                         ("echo $'\\'' ; npm install x3 ; echo '", "npm install x3"),
                         ("if npm install x1; then echo; fi", "if npm install x1"),  # hits carry the raw text
                         ("{ npm install x2; }", "{ npm install x2"),
                         ("cat <<EOF\nx\nEOF\nnpm install after", "npm install after")):
            self.assertIn((4, seg), hits(cmd), cmd)

    def test_heredoc_is_data_only_for_known_readers(self):  # r1 should_fix: unknown readers may run it
        for cmd in ("git commit -F - <<'EOF'\nfix: npm install x\nEOF", "git commit --file=- <<'EOF'\nnpm install x\nEOF",
                    "tee notes.md <<'EOF'\nnpm install x\nEOF"):
            self.assertEqual(hits(cmd), [], cmd)
        for cmd in ("sudo -s <<'EOF'\nnpm install x\nEOF", "sudo -u root -i <<'EOF'\nnpm install x\nEOF",
                    "su <<'EOF'\nnpm install x\nEOF", "fish <<'EOF'\nnpm install x\nEOF",
                    "some-repl <<'EOF'\nnpm install x\nEOF", "tee s.sh <<'EOF'\nnpm install x\nEOF\nbash s.sh",
                    "cat <<'EOF' | sudo -s\nnpm install x\nEOF", "(cat <<'EOF') | su\nnpm install x\nEOF",
                    "foremind log b.1 <<'EOF' | xargs -0 zz\nnpm install x\nEOF",
                    "git commit -F f <<'EOF'\nnpm install x\nEOF"):
            self.assertIn((4, whole(cmd)), hits(cmd), cmd)

    def test_bypass_forms(self):  # N-3; a hit carries the command as written (m2a.5.F1)
        for cmd, seg in (("/usr/bin/npm install r", "/usr/bin/npm install r"),
                         ("sudo -u root -E npm install s", "sudo -u root -E npm install s"),
                         ("sudo -Eu root -g staff --user=root npm install s",
                          "sudo -Eu root -g staff --user=root npm install s"),
                         ("sudo --user root npm install s", "sudo --user root npm install s"),
                         ("env -u HOME -i A=1 npm install s", "env -u HOME -i A=1 npm install s"),
                         ('"npm" install s', "npm install s"),
                         ("npm \\\ninstall s", "npm install s"),
                         ("bash -lc 'npm install t && echo'", "bash -lc npm install t && echo"),
                         ("/bin/sh -ec 'cd x; npm install t'", "/bin/sh -ec cd x; npm install t"),
                         ("zsh -c -- \"eval 'npm install u'\"", "zsh -c -- eval 'npm install u'"),
                         ("sudo bash -c 'sudo -E npm install u'", "sudo bash -c sudo -E npm install u")):
            self.assertIn((4, seg), hits(cmd), cmd)
        self.assertIn((20, "sh -c npm publish"), hits("sh -c 'npm publish'"))
        self.assertIn((4, "npm install a; echo"), hits("npm install a; echo"))  # the whole command too
        self.assertEqual(hits("bash -c"), [])
        self.assertEqual(hits("echo npm install x"), [])

    def test_zsh_forms(self):  # seats run under zsh: =cmd expansion, precommand modifiers, repeat
        for cmd in ("=npm install x", "noglob npm install x", "nocorrect npm install x", "builtin npm install x",
                    "repeat 2 npm install x", "- npm install x", "coproc npm install x"):
            self.assertIn((4, cmd), hits(cmd), cmd)

    def test_shell_syntax_fails_closed(self):  # r1 should_fix: $'…', $"…", braces, globs, substitutions
        for cmd in ("$'npm' install x", "$'\\x6epm' install x", '$"npm" publish', "{npm,install} left-pad",
                    "n?m install x", "$(which npm) install x", "`echo npm` install x"):
            v = ev(cmd)
            self.assertEqual(v.pending, [], cmd)
            [r] = v.matrix
            self.assertIn("命令名里有未展开的 shell 写法", r, cmd)
        v = ev("> log npm install x")  # r2: a redirection is no word wherever it stands, so this is `npm install x`
        self.assertEqual(v.matrix, [])
        self.assertEqual([(h.category, h.values) for h in v.pending], [(4, ["> log npm install x"])])
        # REQ-1: a hit only through shell syntax is a matrix denial, no pending (nobody can approve a variable)
        for cmd, cat, word in (("npm $'install' x", 4, "$install"), ("npm i{nstall,} x", 4, "i{nstall,}"),
                               ("git $'push' origin", 23, "$push"), ("gh $'pr' create", 23, "$pr"),
                               ("git -C . `echo push`", 23, "`echo"), ("pip $'install' -r r.txt", 4, "$install"),
                               ("python3 $S/x.py", 4, "$S/x.py"), ('python3 "$D"/check.py', 4, "$D/check.py"),
                               ("python3 $PWD/x.py", 4, "$PWD/x.py"),
                               ("git -C $D $X", 23, "$X"), ("gh pr $C", 23, "$C")):  # r3 note: the subcommand's word
            v = ev(cmd)
            self.assertEqual(v.pending, [], cmd)
            self.assertTrue(any(f"只因 `{word}` 里未展开的 shell 写法才可能命中 #{cat}" in r and "不生成待决" in r
                                for r in v.matrix), (cmd, v.matrix))
        # literal hits stay pending however much else is a variable
        for cmd, hit in (("npm install $PKG", (4, "npm install $PKG")), ("git -C $D push", (23, "git -C $D push")),
                         ("python3 -m pip install $P", (4, "python3 -m pip install $P"))):
            v = ev(cmd)
            self.assertIn(hit, [(h.category, h.values[0]) for h in v.pending], cmd)
            self.assertEqual(v.matrix, [], cmd)
        for cmd in ("npm run $SCRIPT", "echo $HOME", '"$(git rev-parse --show-toplevel)"/scripts/check.sh',
                    "[ -f x ] && echo y", "[[ -n $x ]]", "{ echo a; }", "git -C $D status", "ls *.py > out",
                    "python3 -m unittest $T"):
            v = ev(cmd)
            self.assertEqual((v.pending, v.matrix), ([], []), cmd)

    def test_parser_failure_denies_the_whole_command(self):  # m2a.5 r3: the old operator split kept quotes and eval
        deep = "$(" * 3000 + "echo a" + ")" * 3000
        for cmd, ctx in ((deep, contextlib.nullcontext()),
                         ("eval 'npm install a'", mock.patch.object(guard, "_parse", side_effect=ValueError("boom")))):
            with ctx:
                v = ev(cmd)
            self.assertEqual((v.pending, v.matrix), ([], [guard.DEEP_REASON]), cmd[:20])
            self.assertIn("命令嵌套太深，拆开再执行", v.matrix[0])

    def test_interpreter_heredoc_is_data(self):  # m2b.2 item 1 (m2a.5 r3 note): their program is outside the guard
        for cmd in ("python3 - <<'EOF'\nimport sys\nprint(sys.argv[1])\nEOF",
                    "python3.13t - x <<'EOF'\nprint(f'{1}')  # $(no)\nnpm install x\nEOF",
                    "PYTHONPATH=. python3 <<'EOF' 2>&1 | tail -3\nfor p in glob('*'): print(p)\nEOF",
                    "node - <<'EOF'\nconsole.log(`${1}`)\nEOF", "ruby <<'EOF'\nputs [1].map { |x| x }\nEOF",
                    "perl - a <<'EOF'\nprint $ARGV[0];\nEOF", "sudo -u me python3 - <<'EOF'\nprint(1)\nEOF",
                    "cd x && python3 - <<'EOF' > out.txt\nprint(1)\nEOF\necho done",
                    # r1: no `-`, and a body that looks like shell syntax or a bad redirection to the whole command
                    "python3 <<'EOF'\nprint(sys.argv[1])\nEOF", "python3 <<'EOF'\nprint(1 > -1)\nEOF",
                    "cd a && python3 <<'EOF'\n[p for p in '*?']\nEOF"):
            v = ev(cmd)
            self.assertEqual((v.pending, v.matrix), ([], []), cmd)
        # an unquoted delimiter: the shell still runs the substitutions in the body first
        self.assertIn((4, "npm install y"), hits("python3 - <<EOF\nprint('$(npm install y)')\nEOF"))
        self.assertIn((4, "npm install y"), hits("node <<EOF\n`npm install y`\nEOF"))
        # the body piped on by something else stays commands; a shell elsewhere in the command no longer makes an
        # interpreter's body commands (REQ-20: it is the program, outside the guard as its output is)
        self.assertIn((4, whole(c := "cat <<'EOF' | python3 -\nnpm install x\nEOF")), hits(c))
        for cmd in ("python3 - <<'EOF'\nnpm install x\nEOF\nbash s.sh", "python3 - <<'EOF' | sh\nnpm install x\nEOF"):
            self.assertEqual(hits(cmd), [], cmd)

    def test_redirections(self):  # m2b.2 item 2 and Q-8: a redirection is no argument and no subcommand
        for cmd in ("git --help 2>&1 | sed -n 1,12p", "git -C . --no-pager 2>/dev/null log", "git > push",
                    "git -P >&2 status", "gh 2>&1 pr view"):
            v = ev(cmd)
            self.assertEqual((v.pending, v.matrix), ([], []), cmd)
        for cmd in ("pip install -r requirements.txt 2>&1 | tail -1", "pip install -r requirements.txt > log",
                    "pip install -r requirements.txt >log 2>&1", "pip install . 2> err.txt", "cat x <&3",
                    "python3 -m pip install -e . >&2"):
            self.assertEqual(hits(cmd), [], cmd)
        for cmd in ("git 2>&1 push", "git -C . >x push origin"):
            self.assertIn(23, [c for c, _ in hits(cmd)], cmd)
        for cmd, cat in (("npm 2>&1 install left-pad", 4), ("npm >/dev/null install x", 4), ("npm > log install x", 4),
                         ("pip 2>&1 install left-pad", 4), ("npm 2>/dev/null publish", 20),
                         ("terraform >log destroy", 19)):  # r1 must_fix: patterns see the command without them
            self.assertIn((cat, cmd), hits(cmd), cmd)
        # a quoted '>' is an argument the command gets (r3)
        self.assertIn(4, [c for c, _ in hits("pip install -r requirements.txt '>' --index-url=https://x.test")])

    def test_redirections_anywhere(self):  # r2 must_fix 1/2: before the name, before the subcommand, before -m/-c
        white = ["2>/dev/null foremind catalog apply out.json", "</dev/null foremind plan approve p",
                 "> /tmp/x foremind init", "foremind 2>/dev/null catalog apply x", "foremind >/tmp/x plan approve p",
                 "python3 2>&1 -m foremind catalog apply x", "python3 > /tmp/x -m foremind init",
                 "bash 2>/dev/null -c 'foremind plan approve p'", "FOO=1 2>&1 sudo 2>&1 foremind resume",
                 "foremind review &>/dev/null --request-changes", "bash <<< x -c 'foremind init'"]
        for cmd in white:
            [r] = ev(cmd, role="seat").matrix
            self.assertIn("按角色白名单", r, cmd)
        for cmd, cat in (("2>/dev/null git push", 23), ("bash 2>&1 -c 'git push'", 23), ("git &>/dev/null push", 23),
                         (">/dev/null npm install left-pad", 4), ("sh >/dev/null -c 'npm install left-pad'", 4),
                         ("python3 2>&1 -m pip install left-pad", 4), ("npm &>/dev/null install left-pad", 4),
                         ("npm >| x install left-pad", 4), ("npm >! x install left-pad", 4)):
            self.assertIn(cat, [c for c, _ in hits(cmd)], cmd)
            self.assertEqual(ev(cmd).matrix, [], cmd)
        # one that cannot be taken out (no target, or `-x` next) denies the whole command
        for cmd in ("bash > -x -c 'git push'", "python3 > -x -m foremind status", "sudo -u > -x ls", "echo >"):
            r = ev(cmd).matrix[0]  # the kept `>` may also make a hit only through shell syntax (REQ-1)
            self.assertIn("看不出是重定向还是参数", r, cmd)

    def test_quoted_redirection_is_an_argument(self):  # r3 must_fix 1: judged before the quotes go
        for cmd in ("git --attr-source '>x' push", 'git --work-tree ">x" push', "git -c '2>&1' push",
                    "git 2>'/dev/null' push"):
            self.assertIn(23, [c for c, _ in hits(cmd)], cmd)
        [r] = ev("git \\>x push").matrix  # `>x` may be the subcommand: denied, not pending (REQ-1)
        self.assertIn("可能命中 #23", r)
        self.assertIn(4, [c for c, _ in hits("env -u '>x' npm install left-pad")])
        for cmd in ("env -u '>x' foremind review b.1 --request-changes", "python3 -X '>x' -m foremind catalog apply x",
                    "exec -a '<x' foremind plan approve p", "bash --rcfile '>x' -c 'foremind init'"):
            [r] = ev(cmd, role="seat").matrix
            self.assertIn("按角色白名单", r, cmd)
        for cmd in ("echo '>'", 'echo ">" x', "grep -n '2>&1' a.py", "foremind review b.1 2>'err.txt'"):
            v = ev(cmd, role="seat")
            self.assertEqual((v.pending, v.matrix), ([], []), cmd)

    def test_process_substitution_denies(self):  # r3 must_fix 2: not parsed at all
        for cmd in ("git -C > >(a b) x push", "diff <(git show HEAD:a.py) <(git show HEAD~1:a.py) > d.txt",
                    "pip install -r <(cat x)", "pip install -r requirements.txt >(cat)", "cat < <(npm install x)",
                    "source <(python3 - <<'EOF'\nnpm install x\nEOF\n)", "bash <<'EOF'\ndiff <(a) b\nEOF",
                    "echo '<(x)'", "bash -c 'tee >(sh)'"):
            for role in ("seat", "controller"):
                v = ev(cmd, role=role)
                self.assertEqual((v.pending, v.matrix), ([], [guard.PROC_REASON]), cmd)
        for cmd in ("cat <<'EOF' > /tmp/x.md\ndiff <(a) <(b)\nEOF", "foremind log b.1 <<'EOF'\n`>(…)` 当参数\nEOF"):
            v = ev(cmd, role="seat")
            self.assertEqual((v.pending, v.matrix), ([], []), cmd)
        self.assertEqual(ev("diff <(a) <(b)", session=None).matrix, [])  # the user's own session

    def test_git_global_options_with_a_value(self):  # m2b.2 item 4 (git.c: take the next word)
        for cmd in ("git --shallow-file push log", "git --attr-source push status", "git --super-prefix push diff"):
            self.assertEqual(hits(cmd), [], cmd)
        self.assertIn((23, "git --attr-source HEAD push"), hits("git --attr-source HEAD push"))
        self.assertEqual(ev("git --shallow-file alias.x config --get user.name").matrix, [])
        self.assertTrue(ev("git --attr-source x config alias.p push").matrix)

    def test_python_names(self):  # m2b.2 item 5: one regex for `python* -m foremind` and `-m pip`
        for exe in ("python3.13t", "python3-intel64", "python3.11", "python"):
            self.assertTrue(ev(f"{exe} -m foremind plan approve p").matrix, exe)
            self.assertEqual(hits(f"{exe} -m pip install -r requirements.txt"), [], exe)
            self.assertEqual(hits(f"{exe} -m pip install left-pad"), [(4, f"{exe} -m pip install left-pad")], exe)


class DeclaredInstallTest(unittest.TestCase):  # N-5
    def test_declared_installs_are_not_4(self):
        for cmd in ("pip install -r requirements.txt && pytest", "pip3 install --requirement=requirements-dev.txt",
                    "python3 -m pip install -e .", "python -m pip install -e '.[dev]'", "pip install .",
                    "pip install -q '.[dev,test]'", "uv pip install -r requirements.txt",
                    "pip install -r requirements.txt -e ./lib",
                    "sudo pip install -rrequirements.txt", "python3.11 -m pip install --editable=.",
                    "pip install -U -r requirements/dev.txt --no-deps -qq"):
            self.assertEqual(hits(cmd), [], cmd)

    def test_anything_else_stays_4(self):
        for cmd in ("pip install -r requirements.txt left-pad", "pip install -e git+https://example.com/x.git#egg=x",
                    "pip install -r https://example.com/req.txt", "pip install -r r.txt --index-url=https://x.test",
                    "pip install -r r.txt -i https://x.test", "pip install requests", "uv pip install requests",
                    "pip install -r r.txt -c constraints.txt",
                    # r1 must_fix: only a path inside the repo; no other option however written
                    "pip install -r /etc/x", "pip install -r ../x", "pip install -r a/../../x", "pip install -e ~/lib",
                    "pip install -r $F", "pip install -r r.txt --find-links=./w",
                    "pip install -r r.txt --constraint=c.txt", "pip install -r r.txt -cc.txt", "pip install -r",
                    "pip install -r r.txt --pre", "pip install -qr r.txt", "pip install --req r.txt",
                    # r2: -r only a manifest #4 tracks by path; judged on the words, not the re-split joined text
                    "pip install -r deps.txt", "pip install -r lib/reqs.txt", "pip install --requirement=r.txt",
                    "pip install -r 'requirements.txt # x'", "pip install -r 'requirements.txt;x'"):
            self.assertTrue(hits(cmd), cmd)
        self.assertEqual(hits("pip install -r requirements.txt && pip install left-pad"),
                         [(4, "pip install left-pad")])
        # two simple commands joining to the same text: pip reads the second as file ` requirements.txt`
        self.assertEqual(hits("pip install -r requirements.txt; pip install '-r requirements.txt'"),
                         [(4, "pip install -r requirements.txt")])

    def test_manifest_edit_still_4(self):  # m2b.2 item 9: one list with the gate (M1-6-r2 N6)
        for path, pat in (("requirements.txt", "*/requirements*.txt"), ("bun.lockb", "*/bun.lockb"),
                          ("sub/gradle/libs.versions.toml", "*/*gradle/libs.versions.toml"), ("mix.lock", "*/mix.lock"),
                          ("pixi.toml", "*/pixi.toml"), ("yarn.lock", "*/yarn.lock")):
            v = guard.evaluate("Write", {"file_path": "/w/api/" + path}, "/", session=SESSION, batch=BATCH,
                               header={}, cfg={})
            self.assertEqual([(h.category, h.pattern) for h in v.pending], [(4, pat)], path)
        self.assertIs(gate.is_manifest, manifests.is_manifest)
        self.assertEqual(hits("pip install -r requirements/dev.in -r constraints.txt"), [])


DENIED = ["foremind plan approve p1", "foremind plan amend p1 --reason r --user-approved",
          "foremind plan amend p1 --user --reason r", "foremind do 'x' --owns api:a --accept t",
          "foremind decide Q-3 1", "foremind decide Q-3 1 --note ok", "foremind decide --note show Q-3 1",
          "foremind decide --void Q-3 --reason r", "foremind decide --vo=Q-3", "foremind confirm-exit b.1",
          "foremind quota --reset", "foremind quota --res --group long", "foremind resume", "foremind init",
          "foremind uninstall", "foremind doctor --rescan", "foremind doctor --rescan --yes",
          # r1 (controller): Claude Code runs the hooks itself; --author could sign as the user; --run-reviewer is the
          # supervisor's
          "foremind hook PreToolUse", "FOREMIND_SESSION= foremind hook PostToolUse", "foremind log b.1 --author user",
          "foremind log b.1 --file f.md --au=user", 'foremind log b.1 --file "$TMPDIR/x.md"',
          "foremind review b.1 --run-reviewer", "FOREMIND_SESSION= foremind review b.1 --run",
          # prefixes and wrappers (N-4: FOREMIND_SESSION is trivially unset inside a command)
          "env -u FOREMIND_SESSION foremind confirm-exit b.1", "FOREMIND_SESSION= foremind decide Q-1 1",
          "env -i PATH=/usr/bin foremind resume", "sudo -E /opt/bin/foremind resume",
          "python3 -P -m foremind plan approve p", "python3.11 -m foremind init", "python3 -Pm foremind resume",
          "python -mforemind uninstall", "python3 -m foremind.__main__ init", "command foremind uninstall",
          "exec foremind do x", "nohup foremind resume", "time foremind init", "bash -c 'foremind plan approve p'",
          "cd /x && foremind resume",
          # words the shell still expands (fail-closed)
          "foremind pl{an,} approve p", "foremind $'plan' approve p", "foremind plan appr* p", "foremind decide $Q 1",
          "foremind plan amend p --user-appr{oved,}", "foremind plan amend p --reason \"$(cat r.md)\"",
          "foremind quota $R", "foremind doctor --res*", "foremind decide show $Q", "foremind decide --note x `cat a`",
          "foremind $X", "foremind decide --new --void Q-3",
          # an empty, missing or unexpanded --notify value; anything besides it
          "foremind decide Q-3 1 --notify=", "foremind decide Q-3 1 --notify ''", 'foremind decide Q-3 1 --noti "$Q"',
          "foremind decide Q-3 1 --notify", "foremind decide --notify $Q", "foremind decide Q-3 1 --notify Q-3",
          # argparse may skip a `--` before the subcommand
          "foremind -- do x", "foremind plan -- approve p", "python3 -m foremind -- init",
          "foremind decide Q-3 1 -- --new",
          # m2b.2: a whitelist, so what is not listed is denied, new commands included
          "foremind catalog apply out.json", "foremind update b.1", "foremind seat b.1", "foremind pause",
          "foremind tick", "foremind supervise", "foremind", "foremind -h", "foremind decide show Q-3 1",
          "foremind decide --question q $X --new", "foremind decide --new --question q extra",
          "foremind decide --n --question q", "foremind decide --new -q x", "foremind quota '>' --reset",
          "foremind plan freeze p", "foremind plan new req", "foremind future-command x"]
EVERY_ROLE = ["foremind decide --new --question q --option a --option b --category 8", "foremind decide",
              "foremind decide show Q-3", "foremind decide --new --question Q-3 --category 12",
              "foremind decide --notify Q-3", "foremind decide --ne --question q --reason 'r?' --blocks b.1",
              "foremind log b.1 --file f.md", "foremind handoff --write b.1", "foremind status", "foremind version",
              "foremind plan validate p --apply", "foremind plan show p", "foremind quota", "foremind quota --group long",
              "foremind doctor", "foremind doctor --yes", "echo foremind plan approve",
              "python3 -m unittest discover -s tests", "python3 -c 'print(1)' -m foremind init",
              "foremind log b.1 <<'EOF'\nasked the user to run foremind plan approve p1\nEOF",
              f"PYTHONPATH={claude._PKG_PARENT} python3.14 -P -m foremind log m2b.2 --file /tmp/x.md",
              "foremind statusline", "foremind release-check v1..main --repo api",
              "foremind report x", "foremind ask 'why?'",
              # shell syntax where it cannot turn into anything else
              "foremind decide --new --question 'ok?' --path 'api:src/*' --command 'npm install left-*' --category 8",
              "foremind decide --notify=Q-3", "foremind decide --noti Q-12", "foremind log b.1 < note.md",
              "foremind decide 2>&1 | head", "foremind quota > q.txt", "foremind doctor 2>/dev/null",
              "foremind status >&2", "foremind decide --new --question $Q"]
BY_ROLE = {"seat": (["foremind review b.1", "foremind review", "foremind gate b.1 --rerun", "foremind review m2b.2 >x"],
                    ["foremind review b.1 --request-changes --item x", "foremind review --req", "foremind review $B",
                     "foremind review --item x b.1", "foremind review b.1 b.2", "foremind review -- b.1",
                     "foremind run b.1", "foremind say x", "foremind plan submit p --dir d",
                     "foremind plan amend p --reason r"]),
           "controller": (["foremind review b.1 --request-changes --item 'x?'", "foremind review b.1 --req --item x",
                           "foremind plan amend p --reason r --batch f.md", "foremind run b.1 b.2", "foremind say hi"],
                          ["foremind review b.1", "foremind review $B --request-changes", "foremind gate b.1",
                           "foremind plan amend p --reason r --user", "foremind plan submit p --dir d"]),
           "planner": (["foremind plan submit p --dir d", 'foremind plan submit p --dir "$D"'],
                       ["foremind plan amend p --reason r", "foremind review b.1", "foremind gate b.1"]),
           "reviewer": ([], ["foremind review b.1", "foremind gate b.1", "foremind run b.1", "foremind plan submit p"]),
           None: ([], ["foremind review b.1", "foremind gate b.1", "foremind run b.1", "foremind plan submit p"])}


class WhitelistTest(unittest.TestCase):  # m2b.2 item 7 (replaces the user-only list of M1-3-r1 N-4, m2a.5)
    def test_denied_for_every_role(self):
        for role in (None, "seat", "controller", "planner", "reviewer"):
            for cmd in DENIED:
                v = ev(cmd, role=role)
                self.assertEqual(v.pending, [], cmd)
                self.assertEqual(len(v.matrix), 1, (role, cmd))
                self.assertIn("按角色白名单", v.matrix[0])
        v = ev("foremind plan approve p1", session="fm-shop-x-1", batch=None, header={}, role="controller")
        self.assertTrue(v.matrix)  # every Foremind session, not only seats
        r, pip = ev("python3 -m $M plan approve p").matrix  # $M may be pip too: a #4 through shell syntax (REQ-1)
        self.assertIn("按角色白名单", r)
        self.assertIn("可能命中 #4", pip)
        self.assertIn("gate", ev("foremind run b.1", role="seat").matrix[0])  # the reason lists what the role may

    def test_allowed_by_role(self):
        for role, (ok, no) in BY_ROLE.items():
            for cmd in EVERY_ROLE + ok:
                self.assertEqual(ev(cmd, role=role).matrix, [], (role, cmd))
            for cmd in no:
                self.assertTrue(ev(cmd, role=role).matrix, (role, cmd))
        self.assertEqual(ev("foremind plan approve p1", session=None).matrix, [])  # the user's own session

    def test_planner_commits_nothing(self):  # m2b.9 r1, Q-21: a cd into the main checkout does not change that
        for cmd in ("git add .", "git commit -m x", "cd /proj && git commit -am x", "git -C /proj commit -m x",
                    "git -c user.name=x commit -m x", "git --git-dir=/proj/.git add a.py", "sudo git add a", "git $X"):
            r = ev(cmd, role="planner").matrix[0]  # `git $X` may also be a push (REQ-1)
            self.assertIn("规划者不提交", r, cmd)
        for cmd in ("git status", "git log --grep commit", "git -C /proj diff", "git show HEAD:add"):
            self.assertEqual(ev(cmd, role="planner").matrix, [], cmd)
        for role in ("seat", "controller", None):
            self.assertEqual(ev("git -C /proj commit -m x", role=role).matrix, [], role)

    def test_session_rewrite_denied(self):  # r2 should_fix 3: log/handoff sign and lock by FOREMIND_SESSION
        for cmd in ("FOREMIND_SESSION= foremind log b.1 --file f.md", "env -u FOREMIND_SESSION foremind log b.1",
                    "env -uFOREMIND_SESSION foremind status", "env --unset=FOREMIND_SESSION foremind status",
                    "env FOREMIND_SESSION=fm-x-1 foremind handoff --accept b.1",
                    "FOREMIND_SESSION=fm-x-1 python3 -m foremind handoff --write b.1",
                    "env -i PATH=/usr/bin foremind log b.1 --file f.md", "/usr/bin/env foremind status",
                    "unset FOREMIND_SESSION; foremind log b.1 --file f.md",
                    "export FOREMIND_SESSION=user && foremind log b.1",
                    "FOREMIND_SESS''ION= foremind status", "sudo foremind log b.1 --file f.md",
                    "exec -c foremind log b.1 --file f.md", "env -i bash -c 'foremind log b.1 --file f.md'",
                    "bash -c 'unset FOREMIND_SESSION; foremind log b.1'",
                    # r3 should_fix 3: an assignment on its own is no command, yet exported for what follows
                    "FOREMIND_SESSION=user; foremind log b.1 --file f.md", "FOREMIND_SESSION=user\nforemind log b.1",
                    "FOREMIND_SESSION=x && python3 -m foremind handoff --accept b.1"):
            for role in ("seat", None):
                [r] = ev(cmd, role=role).matrix
                self.assertIn("不能改写 FOREMIND_SESSION", r, cmd)
        for cmd in ("echo $FOREMIND_SESSION", "unset FOREMIND_SESSION", "FOREMIND_PROJECT=/x foremind status",
                    "FOREMIND_SESSION=x; ls",
                    "env FOO=1 python3 -m unittest", "foremind log b.1 <<'EOF'\nFOREMIND_SESSION 为空时算用户\nEOF"):
            self.assertEqual(ev(cmd, role="seat").matrix, [], cmd)
        self.assertEqual(ev("FOREMIND_SESSION= foremind log b.1", session=None).matrix, [])

    def test_foremind_runs_the_main_checkout(self):  # REQ-2, finding 28: `python3 -m foremind` in a worktree
        main = claude._PKG_PARENT
        for cmd in ("python3 -m foremind log b.1 --file f.md", f"PYTHONPATH={main} python3 -m foremind status",
                    "PYTHONPATH=/x python3 -P -m foremind status", "PYTHONPATH=. python3 -Pm foremind status",
                    f"python3 -P -m foremind log b.1 PYTHONPATH={main}",
                    f"PYTHONPATH={main} bash -c 'PYTHONPATH=. python3 -P -m foremind status'",
                    # r2: each command by its own words, not the raw text commands under bash -c share
                    f"PYTHONPATH={main} bash -c 'python3 -P -m foremind status'",
                    f"bash -c 'PYTHONPATH={main} python3 -P -m foremind status; "
                    "PYTHONPATH=. python3 -P -m foremind log b.1'",
                    # r3: the last PYTHONPATH counts, found past an assignment whose value ends in python*
                    f"PYTHONPATH={main} X=/tmp/python PYTHONPATH=. python3 -P -m foremind status"):
            for role in ("seat", "controller", None):
                [r] = ev(cmd, role=role).matrix
                self.assertIn("固定走主检出代码", r, cmd)
                self.assertIn(claude.foremind_command(), r)
        for cmd in (f"PYTHONPATH={main} python3 -P -m foremind status",
                    f"PYTHONPATH={main}/ python3 -Pm foremind status",
                    f"PYTHONPATH={main} /usr/bin/python3 -P -m foremind status", "foremind status",
                    claude.foremind_command("log", "b.1", "--file", "f.md"),
                    f"bash -c 'python3 x.py && PYTHONPATH={main} python3 -P -m foremind log b.1'",
                    f"bash <<'EOF'\nPYTHONPATH={main} python3 -P -m foremind status\nEOF"):
            self.assertEqual(ev(cmd, role="seat").matrix, [], cmd)
        self.assertEqual(ev("python3 -m foremind status", session=None).matrix, [])  # the user's own session

    def test_main_checkout_named_python(self):  # m2c.1 r3 must_fix: the assignment is no interpreter word
        for main in ("/x/python-foremind", "/x/python", "/x/python3.11"):
            with mock.patch.object(claude, "_PKG_PARENT", Path(main)):
                self.assertEqual(ev(claude.foremind_command("status"), role="seat").matrix, [], main)
                [r] = ev("PYTHONPATH=. python3 -P -m foremind status", role="seat").matrix
                self.assertIn("固定走主检出代码", r, main)

    def test_main_checkout_with_a_space(self):  # m2c.1 r1 must_fix: raw has lost the quotes foremind_command adds
        with mock.patch.object(claude, "_PKG_PARENT", Path("/x/Mobile Documents/foremind")):
            for cmd in (claude.foremind_command("log", "b.1", "--file", "f.md"),
                        "PYTHONPATH='/x/Mobile Documents/foremind/' python3 -P -m foremind status"):
                self.assertEqual(ev(cmd, role="seat").matrix, [], cmd)
            for cmd in ("PYTHONPATH='/x/Mobile Documents/foremindX' python3 -P -m foremind status",
                        "PYTHONPATH='/x/Mobile Documents/foremind' python3 -m foremind status",
                        "PYTHONPATH=/x/Mobile python3 -P -m foremind status"):
                [r] = ev(cmd, role="seat").matrix
                self.assertIn("固定走主检出代码", r, cmd)


class GitOutputTest(unittest.TestCase):  # m2a.1 r2/r3
    def test_output_denied(self):
        for cmd in ("git log -1 --format=%B --output=/etc/x", "git diff --output x", "git -C . show HEAD --output=f",
                    "git log '--output=x'", "git log --outp=x", "cd a; git diff HEAD --output ../b",
                    "git log --$'output'=x", "git diff --out{put,}=x"):
            [r] = ev(cmd).matrix
            self.assertIn("--output", r, cmd)
        for cmd in ("git log --oneline", "git diff --output-indicator-new=+", "git log > out.txt",
                    "git commit -m 'no --output here'"):
            self.assertEqual(ev(cmd).matrix, [], cmd)

    def test_renaming_config_denied(self):  # r2: an alias runs push or --output under another name
        for cmd in ("git -c alias.p=push p", "git -calias.l='log --output=x' l", "git --config-env=alias.p=V p",
                    "git --config-env alias.p=V p", "git -c Alias.P=push P", "git -c include.path=x.cfg p",
                    "git -c includeIf.onbranch:x.path=y p", 'git -c "$X" p', "git config alias.p push",
                    "git config --global alias.p push", "git -C . config set alias.p push", "git config $K push",
                    "gh alias set p 'pr create'", "gh alias import a.yml"):
            v = ev(cmd)
            [r] = v.matrix
            self.assertIn("换个名字执行", r, cmd)
            self.assertEqual(v.pending, [], cmd)
        for cmd in ("git -c color.ui=always log", "git -C alias.d status", "git config user.name",
                    "git config --get core.editor", "gh alias list", "git log --grep alias.p"):
            self.assertEqual(ev(cmd).matrix, [], cmd)


class PushTest(unittest.TestCase):  # N-12
    def test_push_is_a_hit_while_23_is_the_users(self):
        for cmd, seg, pat in (("git push origin fm/b.1", "git push origin fm/b.1", "git push*"),
                              ("git -C ../api -c x=y push", "git -C ../api -c x=y push", "git push*"),
                              ("cd api && gh pr create --fill", "gh pr create --fill", "gh pr create*"),
                              ("sudo gh pr new", "sudo gh pr new", "gh pr create*")):
            [h] = ev(cmd).pending
            self.assertEqual((h.category, h.kind, h.values, h.pattern), (23, "commands", [seg], pat))
        for cmd in ("git commit -m 'push it'", "git log --grep push", "gh pr view", "git pull"):
            self.assertEqual(hits(cmd), [], cmd)

    def test_literal_and_shell_push_under_one_raw(self):  # m2c.1 r1 note: bash -c commands share their raw text
        for cmd in ("bash -c 'git push x; git $Y'", "bash -c 'git $Y; git push x'",
                    "bash -c 'npm install x; npm $Y z'", "bash -c 'npm $Y z; npm install x'"):
            v = ev(cmd)
            self.assertEqual(len(v.pending), 1, cmd)
            self.assertTrue(any("未展开的 shell 写法才可能命中" in r for r in v.matrix), cmd)

    def test_by_delivery_config(self):
        push = "git push"
        system = {"delivery.repo.api.push_pr": "system"}
        self.assertEqual(hits(push, cfg=system), [])
        self.assertTrue(hits(push, cfg={"delivery.repo.api.push_pr": "user"}))
        self.assertTrue(hits(push, cfg=system, header={"repos": ["api", "web"]}))  # web unset = user
        self.assertTrue(hits(push, cfg=system, header=None))  # unreadable header: strictest
        self.assertEqual(hits(push, batch=None, header={}), [])  # only a batch's pushes
        self.assertEqual(hits(push, session=None), [])

    def test_exemption_round_trip(self):  # r1 note: hit → narrow (as decide issues it) → exemptions.find
        root = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        (root / ".foremind" / "exemptions").mkdir(parents=True)
        ts = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
        n = 0
        for cmd, other, cats in (("git push origin fm/b.1", "git push -f origin fm/b.1", [23]),
                                 ("git push origin 'fm/[x]*'", "git push origin fm/xy", [23]),
                                 ("npm install 'left-[a]*'", "npm install left-a", [4]),
                                 # m2a.5.F1: an exemption names the command as written, prefixes included
                                 ("FOO=1 sudo npm install left-pad", "npm install left-pad", [4]),
                                 ("npm install left-pad", "FOO=1 npm install left-pad", [4]),
                                 ("sudo bash -c 'npm install x'", "bash -c 'npm install x'", [4])):
            ids = []
            for h in ev(cmd, root=root).pending:
                n += 1
                ids.append((f"Q-{n}", h.category))
                (root / ".foremind" / "exemptions" / f"Q-{n}.json").write_text(json.dumps(
                    {"batch": BATCH, "category": h.category, "match": narrow({h.kind: h.values}), "expires_at": ts}))
            v = ev(cmd, root=root)
            self.assertEqual([c for _, c in ids], cats, cmd)
            self.assertEqual((v.pending, [(q, h.category) for q, h in v.exempted]), ([], ids), cmd)
            self.assertTrue(ev(other, root=root).pending, other)


class Req20Test(unittest.TestCase):  # m2c.9: misfires and command-name case
    def test_source_leaves_an_interpreter_heredoc_data(self):  # m2b.2 r3 should_fix 4
        body = "import sys\nprint(sys.argv[1])\nif len(x) > 1:\n    print('$(npm install x)')\nEOF"
        for cmd in (f"source .venv/bin/activate && python3 - <<'EOF'\n{body}", f". a && node - <<'EOF'\n{body}",
                    f"bash -c 'ls' && ruby <<'EOF'\n{body}"):
            v = ev(cmd, role="seat")
            self.assertEqual((v.pending, v.matrix), ([], []), cmd)
        for cmd in ("source a && cat <<'EOF' > s.sh\nnpm install x\nEOF", ". a; tee s.sh <<'EOF'\nnpm install x\nEOF"):
            self.assertIn((4, whole(cmd)), hits(cmd), cmd)  # what cat or tee writes a shell may still run

    def test_free_text_is_no_wrapper(self):
        for cmd in ('foremind decide --new --question "run it under env, sudo or exec?" --option a --option b',
                    'foremind log b.1 "env and sudo exec"', "FOO=1 foremind decide --new --question 'sudo x'",
                    f"PYTHONPATH={claude._PKG_PARENT} python3 -P -m foremind decide --new --question 'exec env'"):
            self.assertEqual(ev(cmd, role="seat").matrix, [], cmd)
        for cmd in ("env -i foremind status", "sudo -u foremind foremind log b.1", "exec -c foremind log b.1",
                    "/usr/bin/env foremind decide --new --question x", "sudo bash -c 'foremind log b.1 x'",
                    "env -i bash -c 'foremind decide --new --question env'", "sudo -s <<'EOF'\nforemind log b.1\nEOF"):
            self.assertTrue(any("不能改写 FOREMIND_SESSION" in r for r in ev(cmd, role="seat").matrix), cmd)

    def test_nested_free_text_is_no_wrapper(self):  # m2e REQ-13
        for cmd in ("bash -c 'foremind decide --new --question \"要不要 sudo 还是 env\"'",
                    "sh -c 'foremind log b.1 env sudo exec'", "eval \"foremind log b.1 'run exec or env'\"",
                    "bash -c 'foremind log b.1' env", "bash <<'EOF'\nforemind decide --new --question 'sudo or env?'\nEOF",
                    "bash -c 'sudo echo $(foremind log b.1 x)'",  # the substitution runs in bash, not under sudo
                    "bash -c \"FOO=1 sh -c 'foremind log b.1 exec x'\"",
                    ">/tmp/bash bash -c 'foremind decide --new --question \"要不要 sudo\"'",
                    "bash -c 'foremind log b.1 sudo' >/tmp/bash", "xargs foremind status sudo",
                    "xargs bash -c 'foremind log b.1 sudo'",  # xargs adds what it reads at the end
                    # and puts it for a replace-str: such a word is any word (r2)
                    "xargs -I{} foremind status {} sudo", "xargs -J % foremind status % env",
                    "xargs -I{} bash -c 'foremind decide --new --question \"要不要 sudo\"' {}",
                    "xargs -I{} bash -c 'foremind decide --new --question \"要不要 sudo 还是 env\"' {}"):
            self.assertEqual(ev(cmd, role="seat").matrix, [], cmd)
        # the role whitelist denies its $XARGS first; no _reenv under it either
        cmd = "echo a | xargs -I{} foremind decide --new --question \"要不要 sudo\" --option {}"
        self.assertFalse(any(guard._reenv(w, raw) for w, raw in guard._commands(cmd)))
        for cmd in ("bash -c 'env -u FOREMIND_SESSION foremind log b.1'", "bash -c 'env -u X foremind log b.1'",
                    "eval \"sudo foremind log b.1\"", "bash <<'EOF'\nexec -c foremind log b.1\nEOF",  # its own
                    "env -i bash -c 'foremind log b.1'", "sudo sh -c 'foremind log b.1'",  # a layer around it
                    "bash -c \"sudo bash -c 'foremind log b.1'\"", "eval \"/usr/bin/env -i sh -c 'foremind status'\"",
                    "env -i bash <<'EOF'\nforemind log b.1\nEOF",
                    "cat <<'EOF' | sudo sh\nforemind log b.1\nEOF",  # a body's reader past a pipe
                    "bash <<'EOF'\nsudo -s\nforemind log b.1\nEOF",  # or an earlier line reading the rest
                    "bash -c 'sudo sh' <<'EOF'\nforemind log b.1\nEOF",
                    "bash -c 'uv run -m foremind log b.1' sudo",  # its command word none of its words: all raw
                    # a redirection target or an option's value named like the command word is not it (r1)
                    ">/tmp/bash sudo bash -c 'foremind log b.1'", "> /tmp/bash sudo bash -c 'foremind log b.1'",
                    ">/tmp/sh sudo -s <<'EOF'\nforemind log b.1\nEOF", "xargs -a /tmp/bash sudo bash -c 'foremind log b.1'",
                    "uv run --directory /tmp/bash sudo bash -c 'foremind log b.1'",
                    ">/tmp/foremind sudo foremind log b.1",  # top level too
                    "xargs -I{} sudo foremind status {}", "xargs -I{} env -i bash -c 'foremind log b.1' {}"):
            self.assertTrue(any("不能改写 FOREMIND_SESSION" in r for r in ev(cmd, role="seat").matrix), cmd)
        [r] = ev("bash -c 'foremind log b.1 x; env -i foremind log b.1 y'", role="seat").matrix
        self.assertIn("`foremind log b.1 y`", r)

    def test_command_name_case_on_a_case_insensitive_volume(self):  # m2b.2 r2 note
        with mock.patch.object(guard.pathmatch, "FOLD", True):
            self.assertIn((4, "NPM install x"), hits("NPM install x"))
            self.assertIn((23, "GIT push"), hits("GIT push"))
            [r] = ev("FOREMIND plan approve p", role="seat").matrix
            self.assertIn("按角色白名单", r)
            self.assertTrue(any("不能改写" in r for r in ev("ENV -i foremind status", role="seat").matrix))
        with mock.patch.object(guard.pathmatch, "FOLD", False):
            self.assertEqual(hits("NPM install x"), [])

    def test_configured_capital_command_word(self):  # r1 should_fix 1
        cfg = {"hard_block.patterns": [{"category": 4, "commands": ["R CMD INSTALL *"]}]}
        with mock.patch.object(guard.pathmatch, "FOLD", True):
            for cmd in ("R CMD INSTALL x", "r CMD INSTALL x"):
                self.assertEqual(hits(cmd, cfg=cfg), [(4, cmd)], cmd)
            self.assertEqual([h.pattern for h in ev("R CMD INSTALL x", cfg=cfg).pending], ["R CMD INSTALL *"])
            self.assertEqual(hits("R cmd install x", cfg=cfg), [])  # only the command word folds
        with mock.patch.object(guard.pathmatch, "FOLD", False):
            self.assertEqual(hits("R CMD INSTALL x", cfg=cfg), [(4, "R CMD INSTALL x")])
            self.assertEqual(hits("r CMD INSTALL x", cfg=cfg), [])

    def test_owns_paths_through_pathmatch(self):  # REQ-19
        def matrix(qual, owns):
            with mock.patch.object(guard, "lock_holder", return_value=SESSION):
                return guard._matrix("/wt/x", "worktree", qual, root="/", session=SESSION, batch=BATCH, role="seat",
                                     header={"owns_paths": owns})
        with mock.patch.object(guard.pathmatch, "FOLD", True):
            for qual, owns in (("api:./SRC//a.py", ["api:src/"]), ("api:src/a.py", ["api:./src/*.py"])):
                self.assertEqual(matrix(qual, owns), [], qual)
            self.assertTrue(matrix("api:src/a.py", ["api:src"]))  # only a trailing / owns what is below
        with mock.patch.object(guard.pathmatch, "FOLD", False):
            self.assertTrue(matrix("api:SRC/a.py", ["api:src/"]))


class OutsideTest(unittest.TestCase):  # m2b.2 item 8 (M1-5-r1 N-6)
    def test_a_seat_writes_only_the_temp_dir_outside_the_project(self):
        def matrix(path, **kw):
            kw = {"session": SESSION, "batch": BATCH, "header": {}, "cfg": {}, "role": "seat", **kw}
            return guard.evaluate("Write", {"file_path": path}, "/", **kw).matrix
        home = os.path.expanduser("~/notes/x.md")
        [r] = matrix(home)
        self.assertIn("在项目之外", r)
        self.assertTrue(matrix(home, role=None))  # a session with a batch and no role is taken for the seat
        for p in (os.path.join(tempfile.gettempdir(), "x.py"), "/tmp/x.py", "/private/tmp/claude-1/s/x.md"):
            self.assertEqual(matrix(p), [], p)
        for kw in ({"role": "controller"}, {"batch": None}, {"session": None}):
            self.assertEqual(matrix(home, **kw), [], kw)


class ResidualTest(unittest.TestCase):  # m2d.7, REQ-19 [防对抗] forms ①–④
    MAIN = claude._PKG_PARENT

    def denied(self, cmd, why, role="seat"):
        self.assertTrue(any(why in r for r in ev(cmd, role=role).matrix), (cmd, ev(cmd, role=role).matrix))

    def test_wrappers_keep_the_whitelist_and_the_prefix(self):  # ①
        pre = f"PYTHONPATH={self.MAIN} python3 -P -m foremind"
        for w in ("timeout 5", "timeout -s KILL -k 3 10s", "timeout --signal=TERM --kill-after 1 --foreground 5",
                  "TIMEOUT -vs9 1.5m", "nice", "nice -n 5", "nice -5", "nice --adjustment=3 -n1", "xargs",
                  "xargs -0 -n 1 -P4", "xargs --max-args=2 -r", "uv run", "uv -q run --frozen --with x -p 3.11",
                  "uv --directory d run --env-file=.env", "timeout 5 nice -n 1 xargs -r"):
            self.denied(f"{w} foremind plan approve p1", "按角色白名单")
            self.denied(f"{w} python3 -m foremind status", "固定走主检出代码")
            self.assertTrue(hits(f"{w} git push origin fm/b.1"), w)
            self.assertTrue(hits(f"{w} npm install left-pad"), w)
        for w in ("timeout 5", "nice -n 5", "xargs -r", "uv run --frozen"):  # env goes through, uv's PATH does not
            self.assertEqual(ev(f"PYTHONPATH={self.MAIN} {w} python3 -P -m foremind status", role="seat").matrix, [])
            self.assertEqual(ev(f"{w} git status", role="seat").matrix, [], w)
        for cmd in ("timeout 5 foremind status", "nice foremind report", "xargs foremind status"):
            self.assertEqual(ev(cmd, role="seat").matrix, [], cmd)
        for cmd in ("uv run foremind status", "uv run -m foremind status", "uv run --module foremind status",
                    f"PYTHONPATH={self.MAIN} uv run --env-file e python3 -P -m foremind status",
                    f"UV_ENV_FILE=e PYTHONPATH={self.MAIN} uv run python3 -P -m foremind status",
                    "PATH=/wt/.venv/bin:/usr/bin foremind status"):
            self.denied(cmd, "固定走主检出代码")
        for cmd in (f"env -S '{pre} plan approve p1'", f"env -S'{pre} status'", f"env --split-string='{pre} log b.1'",
                    "env -iS 'foremind status'"):
            self.assertTrue(ev(cmd, role="seat").matrix, cmd)  # env wraps it: a rewrite as before, or the whitelist
        for cmd in ("env -S 'git push origin x'", "env -S\"npm install x\" -v", "env -S '-i git push'",
                    "env -S 'git\\_push\\_origin\\_x'"):
            self.assertEqual(len(ev(cmd).pending), 1, cmd)
        # r1 must_fix: -S split as env splits it (env.c build_argv), `\_` a separator outside double quotes
        self.denied("env -S 'foremind\\_plan\\_approve\\_p1'", "按角色白名单")
        self.denied("env -S 'python3\\_-m\\_foremind\\_status'", "`python3 -m foremind status`")  # split, then env
        self.denied("env -S 'foremind\\qstatus'", "选项认不出")  # env fails on it: unreadable
        for cmd in ("env -S '\"foremind\\_plan\" approve p1'", "env -S 'foremind status \\c plan approve p1'",
                    "env -S 'foremind status #plan approve p1'"):
            self.assertFalse([r for r in ev(cmd, role="seat").matrix if "按角色白名单" in r], cmd)  # no plan approve

    def test_what_xargs_reads_is_shell_syntax(self):  # ①: its input adds words, or stands for a replace-str
        for cmd in ("echo --user-approved | xargs foremind plan amend p --reason r", "xargs foremind review",
                    "xargs -iI foremind plan approve p1", "xargs -I{} foremind {} approve p1"):
            self.denied(cmd, "按角色白名单", role="controller")
        self.denied("echo foremind | xargs -I X X plan approve p1", "命令名里有未展开的 shell 写法")
        for cmd in ("echo -m foremind plan approve p1 | xargs python3", "xargs -I{} python3 {} status",
                    "xargs -J % python3 -% foremind status"):
            self.assertTrue(ev(cmd, role="seat").matrix, cmd)
        for cmd in ("git ls-files | xargs wc -l", "xargs python3 -m py_compile", "xargs -I{} python3 x.py {}",
                    "find . -name '*.pyc' -print0 | xargs -0 rm -f"):
            self.assertEqual(ev(cmd, role="seat").matrix, [], cmd)
        self.denied(f"PYTHONPATH={self.MAIN} xargs --process-slot-var=PYTHONPATH python3 -P -m foremind status",
                    "固定走主检出代码")
        self.assertEqual(hits("xargs npm install"), [(4, "xargs npm install")])  # what it installs comes from stdin

    def test_a_wrapper_it_cannot_read(self):  # the general fallback
        for cmd in ("timeout --bogus 5 foremind status", "timeout soon foremind status", "nice -x",
                    "xargs --bogus python3 -m foremind status", "uv run --bogus python3 x.py", "uv -Z run foremind",
                    "xargs -K", "timeout 5 xargs -Q python3 t.py"):
            v = ev(cmd if "foremind" in cmd or "python" in cmd else cmd + " foremind status", role="seat")
            self.assertTrue(any("选项认不出" in r for r in v.matrix), (cmd, v.matrix))
        for cmd in ("timeout --bogus 5 ls", "xargs --bogus grep x", "uv pip install -r requirements.txt foremind"):
            self.assertFalse(any("选项认不出" in r for r in ev(cmd, role="seat").matrix), cmd)
        self.assertTrue(any("选项认不出" in r or "不能改写" in r for r in ev("env -S 'a \"b' foremind").matrix))

    def test_the_python_path_in_effect(self):  # ②: redirections and wrapper options are no assignments
        m = self.MAIN
        for cmd in (f"PYTHONPATH={m} 2>/tmp/python PYTHONPATH=. python3 -P -m foremind status",
                    f"PYTHONPATH={m} > python3 PYTHONPATH=. python3 -P -m foremind status",
                    f"PYTHONPATH=. xargs -I PYTHONPATH={m} python3 -P -m foremind status",
                    f"PYTHONPATH=. timeout -s PYTHONPATH={m} 5 python3 -P -m foremind status",
                    f"PYTHONPATH={m} PYTHONPATH+=: python3 -P -m foremind status"):
            self.denied(cmd, "固定走主检出代码")
        for cmd in (f"PYTHONPATH={m} 2>/dev/null python3 -P -m foremind status",
                    f"PYTHONPATH=. 2>/tmp/x PYTHONPATH={m} python3 -P -m foremind status >/tmp/python"):
            self.assertEqual(ev(cmd, role="seat").matrix, [], cmd)

    def test_git_subcommand_and_shell_name_fold(self):  # ③
        body = "tee s.sh <<'EOF'\nnpm install x\nEOF\n"
        with mock.patch.object(guard.pathmatch, "FOLD", True):
            self.assertIn((23, "git PUSH origin x"), hits("git PUSH origin x"))
            self.assertIn((23, "git -C . Push"), hits("git -C . Push"))
            self.assertIn("规划者不提交", ev("git ADD .", role="planner").matrix[0])
            self.assertIn("换个名字执行", ev("git CONFIG alias.p push").matrix[0])
            for cmd in (body + "timeout 5 BASH s.sh", body + "watch BASH s.sh"):
                self.assertIn((4, whole(cmd)), hits(cmd), cmd)
        with mock.patch.object(guard.pathmatch, "FOLD", False):
            for cmd in ("git PUSH origin x", body + "watch BASH s.sh"):
                self.assertEqual(hits(cmd), [], cmd)
            self.assertEqual(ev("git ADD .", role="planner").matrix, [])

    def test_python_c_naming_foremind(self):  # ④
        m = self.MAIN
        for cmd in ("python3 -c 'import foremind'", "python3 -Pc 'from foremind import cli'",
                    "python3 -I -c \"__import__('foremind')\"", "python3 -cimport\\ foremind",
                    "python3 -c 'import importlib; importlib.import_module(\"foremind.cli\")'",
                    f"PYTHONPATH=. python3 -P -c 'import foremind'", f"PYTHONPATH={m} python3 -c 'import foremind'",
                    f"timeout 5 python3 -c 'import foremind'", "echo x | xargs -0 python3 -c",
                    "xargs -I{} python3 -c '{}'"):  # r1 note: what xargs reads may be foremind's code
            for role in ("seat", "controller", None):
                [r] = ev(cmd, role=role).matrix
                self.assertIn("`python* -c` 的代码用到 foremind", r, cmd)
                self.assertIn(f"PYTHONPATH={shlex.quote(str(m))} python3 -P -c", r)
        for cmd in (f"PYTHONPATH={m} python3 -P -c 'import foremind'", f"PYTHONPATH={m} python3 -Pc 'import foremind'",
                    "python3 -c 'print(1)'", "python3 -c 'import foremind_x'", "python3 x.py -c foremind",
                    "xargs python3 -c 'print(1)'", f"PYTHONPATH={m} xargs python3 -P -c"):
            self.assertEqual(ev(cmd, role="seat").matrix, [], cmd)
        self.assertEqual(ev("python3 -c 'import foremind'", session=None).matrix, [])  # the user's own session


if __name__ == "__main__":
    unittest.main()
