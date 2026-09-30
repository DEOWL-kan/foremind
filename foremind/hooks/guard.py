"""PreToolUse rules (DESIGN §1.1 hooks and boundary, §2.3 writable matrix, §8.1–8.2, §20 I7 I8 I10 I11 I35–I37).

Two kinds of denial:
- hard blocks — system/host config (#22, fixed), built-in and configured patterns (`hard_block.patterns`).
  Exemptable per batch by an exemption of the same category (exemptions.py); an unexempted hit becomes a pending
  request.
- writable matrix — owns_paths, role, lock holder, and the rest of `.foremind/` (program state, I36). Not
  exemptable: the seat asks for a scope change (#8), a handoff, or uses the foremind command that writes the file.
Non-Foremind sessions (the user's own) get only the `.foremind/` state rule and delivery.toml: their Edit/Write of
the other #22 files goes through and PostToolUse records it (I51). Bash is checked per simple command (_commands):
command patterns, `git push`/`gh pr create` while #23 is the user's (hard blocks; an exemption is looked up with the
command as written, VAR=value, env and sudo kept), and on the matrix side the foremind usages outside the session's
role whitelist (_ALLOWED), a foremind run with FOREMIND_SESSION rewritten, a `python* -m foremind` not pinned to the
main checkout (REQ-2), a pattern or #23 hit only through unexpanded shell syntax (REQ-1: nobody can approve a
variable), git's `--output`, and a planner's git add/commit; redirections (an unquoted operator, _words) are taken
out wherever they stand and wrappers (env, sudo, timeout, nice, xargs, uv run) stripped (_norm): one whose options
cannot be read denies when foremind or python appears in it, as does a `python* -c` naming foremind off the main
checkout (m2d.7).
A process substitution `<(…)`/`>(…)` outside heredoc bodies denies the whole command unparsed, as a parse failure
does (m2b.2 r3). Other writes through Bash cannot be tied to a path here; the gate's diff and L0 hashes catch them
afterwards. What an interpreter (python*, node, ruby, perl) runs — its -c/-e text, a heredoc it reads as its
program — is outside the guard too (§14): only the shell around it is checked.
"""
import os
import re
import shlex
import tempfile
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from foremind import exemptions, lock, pathmatch, review, worktree
from foremind.manifests import MANIFESTS, is_manifest
from foremind.paths import user_config_dir
from foremind.schemas import Q_ID
from foremind.seat import project_slug
from foremind.vendors import claude

EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}

# #22 system and host config (§2.3 last row, §20 I36 I40). Compared lower-cased: on a case-insensitive filesystem
# claude.md is CLAUDE.md.
SYSTEM_GLOBS = ("*/foremind.toml", "*/.foremind/config.toml", "*/.foremind/delivery.toml", "*/.foremind/roles/*",
                "*/.foremind/hooks/*", "*/.foremind/adapters/*", "*/.foremind/sessions/*",
                "*/.claude/settings*.json", "*/claude.md", "*/claude.local.md", "*/agents.md", "*/.codex/*")
DELIVERY_GLOB = "*/.foremind/delivery.toml"  # program-owned: even the user's own session goes through foremind (I51)
DELIVERY_REASON = ("{path}：delivery.toml 由程序独占、整体重写，不手改；交付约定经 `foremind init` 或 "
                   "`foremind doctor --rescan` 的提案确认后写入")
DEEP_REASON = "命令嵌套太深，拆开再执行（解析失败，看不出其中各条命令，整条按可写矩阵拒绝，不可豁免）"
PROC_REASON = ("命令里有进程替换 `<(…)`/`>(…)`：Foremind 会话里整条按可写矩阵拒绝、不解析（不可豁免，不生成待决；"
               "heredoc 数据正文里的不算）；先把输出写进临时文件，再把文件交给命令")
STATE_GLOB = "*/.foremind/*"  # everything else there is program state (I36)
STATE_REASON = ("{path}：.foremind/ 是程序状态，只经 foremind 命令写（可写矩阵 §2.3，不可豁免）：批次日志用 "
                "`foremind log`，交接段用 `foremind handoff --write`，凭据、决策等经 `foremind decide --new` 申请")

_PROJECT = r"\.(\[[\w,.-]*\])?"  # `.` or `.[extras]`
_PIP_FLAGS = re.compile(r"-[qvU]+|--(quiet|verbose|upgrade|no-deps|no-cache-dir)")  # none changes where from or what
_MANIFESTS = ["*/" + g for g in MANIFESTS]  # #4 by path (Edit/Write), so what they list is declared
_PYTHON = re.compile(r"python[\w.-]*")  # python3.13t, python3-intel64, pythonw
# bash's and zsh's redirection operators (`2>&1`, `&>f`, `>|f`, `>!f`, `<<EOF`, `<<<s`); group 2: an attached target
_REDIR = re.compile(r"\d*(<<<|<<-?|<>|<&|<|&?>>?[&|!]{0,2})(.*)", re.S)


class _Redir(str):
    """A word _words read as a redirection: its operator stands unquoted at the start of its raw text."""


class _Cmd(list):
    """A simple command's words (_norm) with `own`, its words as written (VAR=value, env, sudo kept): the raw text
    _commands pairs it with is that of the enclosing command under bash -c or a heredoc a shell reads (`top` False).
    `env`: what it runs with (_norm). `outer`: the words before the command word (_pre) of each command around it — the
    bash -c or eval running it, for a heredoc body's every command before it there (its reader is not known)."""
    own: list
    top: bool
    env: list
    outer: list


def _unredirect(w) -> list | None:
    """`w` without its redirection words (_Redir) and their targets (`2>&1`, `>f`, `> f`): the shell takes them out
    wherever they stand before the command sees its arguments. A quoted `'>x'` is an argument (m2b.2 r3), and so is a
    `<(…)`/`>(…)`. None when a separate target is missing or starts with `-` (fail closed)."""
    out, i = [], 0
    while i < len(w):
        m, i = _REDIR.fullmatch(w[i]) if isinstance(w[i], _Redir) else None, i + 1
        if not m or m.group(2).startswith("("):
            out.append(w[i - 1])
        elif not m.group(2):
            if i == len(w) or w[i].startswith("-"):
                return None
            i += 1
    return out


def _declared(w) -> bool:
    """`pip install` (pip3, python -m pip, uv pip) of only what the repo already declares: -r with a manifest #4
    tracks by path (manifests.py), -e with a directory inside the repo (its manifest is tracked the same way), the
    project itself, and _PIP_FLAGS. Anything else keeps it a #4 (N-5): a path starting with / or ~, a `..` segment,
    shell syntax, `://`, any other option however written. `w`: the words (_norm) of the command the hit is on."""
    if w[:1] == ["uv"] or w and _PYTHON.fullmatch(w[0]) and w[1:3] == ["-m", "pip"]:
        w = w[1:] if w[0] == "uv" else w[2:]
    if w[:1] not in (["pip"], ["pip3"]) or w[1:2] != ["install"]:
        return False
    args, ok = iter(w[2:]), False
    for a in args:
        if _PIP_FLAGS.fullmatch(a):
            continue
        req = a in ("-r", "--requirement") or a.startswith(("--requirement=", "-r"))
        if a in ("-r", "--requirement", "-e", "--editable"):
            a = next(args, "")
        elif a.startswith(("--requirement=", "--editable=")):
            a = a.partition("=")[2]
        elif a.startswith(("-r", "-e")):
            a = a[2:]
        elif not re.fullmatch(_PROJECT, a):
            return False
        if (not re.fullmatch(_PROJECT + r"|[\w.-]+(/[\w.-]+)*/?", a) or ".." in a.split("/")
                or req and not is_manifest(a)):
            return False
        ok = True
    return ok


# Always on (§8.1 rows with a hard block). Manifests carry dev and runtime deps alike, so they file under the
# stricter #4; the seat may refile as #3 through `foremind decide --new`. `unless`: a segment it clears is no hit.
DEFAULT_PATTERNS = [
    {"category": 4,
     "paths": _MANIFESTS,
     "commands": ["npm install *", "npm i *", "npm add *", "yarn add *", "pnpm add *", "pip install *",
                  "pip3 install *", "python* -m pip install *", "uv add *",
                  "uv pip install *", "poetry add *", "cargo add *", "go get *", "gem install *", "bundle add *",
                  "flutter pub add *", "dart pub add *", "brew install *"],
     "unless": _declared},
    {"category": 7, "paths": ["*/migrations/*", "*/db/migrate/*"]},
    {"category": 19, "commands": ["gh repo delete*", "gh release delete*", "terraform destroy*", "kubectl delete*",
                                  "aws s3 rm *", "aws s3 rb *"]},
    {"category": 20, "commands": ["npm publish*", "yarn publish*", "pnpm publish*", "twine upload*", "cargo publish*",
                                  "gem push*", "gh release create*", "docker push*", "kubectl apply*",
                                  "terraform apply*", "helm install*", "helm upgrade*", "fly deploy*",
                                  "firebase deploy*", "vercel*", "flutter pub publish*", "dart pub publish*"],
     # I35: side effects unknown = assumed (§12.3) until the user lists the tool in hard_block.allow_tools
     "tools": ["mcp__*"]},
]


@dataclass
class Hit:
    category: int
    kind: str  # paths | commands | tools
    values: list  # what matched: a path's absolute and qualified forms, one command as written, or the tool name
    pattern: str
    shell: str | None = None  # the word whose unexpanded shell syntax alone makes the hit (REQ-1): a matrix denial

    def reason(self) -> str:
        if self.shell:
            return (f"`{self.values[-1]}`：这条命令只因 `{self.shell}` 里未展开的 shell 写法才可能命中 #{self.category}"
                    f"（规则 `{self.pattern}`）；没人能批准变量命令（不可豁免，不生成待决）。把路径与参数写成字面"
                    "（临时脚本用 Write 写成文件，以绝对路径运行）再执行")
        return (f"{self.values[-1]} 命中硬拦截（授权表 #{self.category}，规则 `{self.pattern}`），没有有效的放行凭据；"
                "已登记待决请求，获批签发凭据前不要绕过")


@dataclass
class Verdict:
    foremind: bool
    pending: list = field(default_factory=list)  # hard-block hits without a valid exemption
    exempted: list = field(default_factory=list)  # (exemption id, Hit)
    matrix: list = field(default_factory=list)  # writable-matrix denial reasons

    @property
    def reasons(self) -> list[str]:
        return [h.reason() for h in self.pending] + self.matrix


def allowed_tools() -> list[str]:
    """`hard_block.allow_tools` from the user config file alone (I35): no project or task layer may widen it."""
    try:
        with open(user_config_dir() / "config.toml", "rb") as f:
            return _strs(tomllib.load(f).get("hard_block", {}).get("allow_tools"))
    except Exception:  # noqa: BLE001 — unreadable = nothing allowed (I38)
        return []


def lock_holder(root, batch) -> str | None:
    try:
        return lock.holder(root, batch)
    except (OSError, ValueError):  # unreadable lock: nobody provably holds it, so nobody writes
        return "（锁文件读不到）"


_HEREDOC = re.compile(r"""<<(-?)[ \t]*((?:'[^']*'|"[^"]*"|\\.|[^\s;&|<>()'"\\])+)""")
_ASSIGN = re.compile(r"[A-Za-z_]\w*\+?=.*", re.S)
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "su"}
# words that run the next word as the command (options skipped: exec -a NAME, time -p, command -v); noglob … - are
# zsh's precommand modifiers and coproc (seats run under zsh)
_WRAPPERS = {"command", "exec", "nohup", "time", "{", "!", "if", "then", "elif", "else", "do", "while", "until",
             "noglob", "nocorrect", "builtin", "coproc", "-"}
# what a word (quotes already gone) may still have the shell expand: parameters and substitutions ($'…' and $"…"
# included), globs, brace lists, redirections. Such a word can turn into anything, so the checks fail closed on it.
_SPECIAL = re.compile(r"[$`*?<>]|\[.*\]|\{.*(,|\.\.).*\}")
_SUDO_LONG = ("--user", "--group", "--prompt", "--close-from", "--chdir", "--host", "--chroot", "--role",
              "--command-timeout", "--type", "--other-user")
# git options before the command that take the next word as their value (git --help, git.c; --super-prefix: older gits)
_GIT_ARG = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--attr-source", "--shallow-file",
            "--super-prefix")
_GIT_HIDE = ("alias.", "include.", "includeif.")  # config keys that rename a subcommand or pull in a config file
_STDIN = "$XARGS"  # what xargs reads and adds to its command: unknown here, so shell syntax (_norm)
_FM = re.compile(r"foremind|python", re.I)  # in a wrapper _norm cannot read, these deny (_bash_matrix)
_DURATION = re.compile(r"[\d.]+[smhd]?|inf(inity)?")
# options of the wrappers _norm reads strictly (_getopt): (getopt's short string, long ones: `=` taking a value, `?`
# one only after `=`); GNU's and BSD's together where they differ, uv's from `uv run --help` (global ones included)
_TIMEOUT = ("s:k:vfp", ("signal=", "kill-after=", "verbose", "foreground", "preserve-status"))
_NICE = ("n:", ("adjustment=",))
_XARGS = ("0a:d:E:e::I:i::J:L:l::n:oP:prR:S:s:tx",
          ("null", "arg-file=", "delimiter=", "eof?", "replace?", "max-lines?", "max-args=", "max-procs=",
           "max-chars=", "interactive", "no-run-if-empty", "verbose", "exit", "show-limits", "open-tty",
           "process-slot-var="))
_ENV = ("0ia:u:C:S:P:v", ("null", "ignore-environment", "argv0=", "unset=", "chdir=", "split-string=", "debug",
                          "block-signal?", "default-signal?", "ignore-signal?", "list-signal-handling"))
_UV = ("msw:UP:p:i:f:C:nqvhV",
       ("extra=", "all-extras", "no-extra=", "no-dev", "only-dev", "group=", "no-group=", "no-default-groups",
        "only-group=", "all-groups", "module", "no-editable", "no-editable-package=", "exact", "env-file=",
        "no-env-file", "with=", "with-editable=", "with-requirements=", "isolated", "active", "no-sync", "locked",
        "frozen", "script", "gui-script", "all-packages", "package=", "no-project", "python-platform=", "index=",
        "default-index=", "index-url=", "extra-index-url=", "find-links=", "no-index", "index-strategy=",
        "keyring-provider=", "upgrade", "upgrade-package=", "upgrade-group=", "resolution=", "prerelease=",
        "prerelease-package=", "fork-strategy=", "exclude-newer=", "exclude-newer-package=", "no-sources",
        "no-sources-package=", "reinstall", "reinstall-package=", "link-mode=", "compile-bytecode",
        "config-setting=", "config-settings-package=", "no-build-isolation", "no-build-isolation-package=",
        "no-build", "no-build-package=", "no-binary", "no-binary-package=", "no-cache", "cache-dir=", "refresh",
        "refresh-package=", "python=", "python-preference=", "managed-python", "no-managed-python",
        "no-python-downloads", "quiet", "verbose", "color=", "system-certs", "native-tls", "offline",
        "allow-insecure-host=", "no-progress", "directory=", "project=", "config-file=", "no-config", "preview",
        "no-preview", "help", "version"))


def _scan(s, i=0, close=None, code=False, bodies=None) -> tuple[list[str], int]:
    """(simple commands, index past `close`) of shell text s[i:]: split on ; & | ( ) and newlines outside quotes,
    `#` comments dropped. Command substitutions — also inside double quotes and unquoted heredoc bodies — and process
    substitutions `<(…)`/`>(…)` add their commands after the top-level ones and stay in their word as raw text; the `&`
    of `>&`/`<&`/`&>` (`2>&1`) and the `|` of `>|` are part of their redirection, not a separator. A heredoc body is
    data only when the command it belongs to reads it as such (_reads_data) and does not pipe it on; with `code` (the
    command runs a shell somewhere, which may run what cat or tee wrote) only an interpreter's (REQ-20). Else its
    lines are commands too. `bodies`, when given, gets (start, end, commands) of each body in `s` (its delimiter line
    included), and a body's commands go there instead of into the result."""
    out, subs, cur, docs = [], [], [], []  # docs: [delimiter, <<-, quoted, owner text once its command is cut]
    q, parens, arith = None, 0, 0

    def cut(sep=""):
        if t := "".join(cur).strip():
            out.append(t)
        for d in docs:
            if d[3] is None:
                # piped on or into a process substitution: no owner reads it, unless an interpreter does (its output
                # is piped on, not the body)
                d[3] = "" if sep in ("|", "(") and not (sep == "|" and _interpreter(_norm(_words(t)))) else t
        cur.clear()
    while i < len(s):
        c = s[i]
        if q in ("'", "$'"):  # $'…' honours backslash escapes, '…' nothing
            n = 2 if c == "\\" and q == "$'" else 1
            cur.append(s[i:i + n])
            q, i = (None if c == "'" else q), i + n
        elif c == "\\":
            cur.append("" if s[i + 1:i + 2] == "\n" else s[i:i + 2])  # a line continuation joins the words
            i += 2
        elif (s.startswith("$(", i) and not s.startswith("$((", i) or c == "`" and close != "`"
              or q is None and not arith and s.startswith(("<(", ">("), i)):
            j = i + (1 if c == "`" else 2)
            inner, i = _scan(s, j, "`" if c == "`" else ")", code, bodies)
            subs += inner
            cur.append(s[j - (1 if c == "`" else 2):i])
        elif q == '"':
            cur.append(c)
            q, i = (None if c == '"' else q), i + 1
        elif c == close and (close == "`" or parens == 0):
            cut()
            return out + subs, i + 1
        elif c in "'\"":
            q = "$'" if c == "'" and cur and cur[-1] == "$" else c
            cur.append(c)
            i += 1
        elif c == "#" and (not cur or cur[-1] in (" ", "\t")):
            i = s.find("\n", i) % (len(s) + 1)  # to the newline, or the end
        elif s.startswith("<<<", i):
            cur.append("<<<")
            i += 3
        elif c == "<" and not arith and (m := _HEREDOC.match(s, i)):
            word = m.group(2)
            delim = re.sub(r"""'([^']*)'|"([^"]*)"|\\(.)""", lambda g: "".join(filter(None, g.groups())), word)
            docs.append([delim, m.group(1) == "-", any(ch in word for ch in "'\"\\"), None])
            cur.append(m.group(0))
            i = m.end()
        elif c == "\n":
            cut("\n")
            i += 1
            for delim, tabs, quoted, owner in docs:
                j = i
                while j < len(s):
                    end = s.find("\n", j) % (len(s) + 1)
                    if (s[j:end].lstrip("\t") if tabs else s[j:end]) == delim:
                        break
                    j = end + 1
                start, body, i = i, s[i:j], min(s.find("\n", j) % (len(s) + 1) + 1, len(s))
                run = [] if (data := _reads_data(owner, code)) else _scan(body, code=True)[0]
                if data and not quoted:
                    subs += _subs(body)
                if bodies is None:
                    out += run
                else:
                    bodies.append((start, i, run))
            docs.clear()
        elif s.startswith("((", i) or arith and s.startswith("))", i):  # arithmetic: its << is no heredoc
            arith, parens = (arith + 1, parens + 2) if c == "(" else (arith - 1, parens - 2)
            cut()
            i += 2
        elif c in "();&|" and not (c == "&" and (cur and cur[-1] in ("<", ">") or s.startswith(">", i + 1))
                                   or c == "|" and cur and cur[-1] == ">"):  # `>|f`: noclobber, no pipe
            parens = parens + 1 if c == "(" else max(parens - (c == ")"), 0)
            cut(c)
            i += 1
        else:
            cur.append(c)
            i += 1
    cut()
    return out + subs, i


def _subs(text) -> list[str]:
    """Commands of the substitutions in heredoc body `text` (quotes there are literal)."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text.startswith("$(", i) or text[i] == "`":
            inner, i = _scan(text, i + (2 if text[i] == "$" else 1), ")" if text[i] == "$" else "`")
            out += inner
        else:
            i += 1
    return out


def _interpreter(w) -> bool:
    """Normalized words `w` run a non-shell interpreter (python*, node, ruby, perl)."""
    return bool(w) and (w[0] in ("node", "ruby", "perl") or bool(_PYTHON.fullmatch(w[0])))


def _reads_data(owner, code=False) -> bool:
    """The simple command `owner` (raw text) reads a heredoc as data: as a program of a non-shell interpreter
    (python*, node, ruby, perl), which the guard leaves alone as it does their -c text (§14); unless `code`, also cat,
    tee, foremind log|handoff, git commit -F -. Any other reader might run it as shell (sudo -s, su, a shell not in
    _SHELLS), so its body counts as commands."""
    w = _norm(_words(owner or ""))
    if not w or _interpreter(w):
        return bool(w)
    return not code and (w[0] in ("cat", "tee") or (_foremind_args(w) or [])[:1] in (["log"], ["handoff"])
                         or w[:2] == ["git", "commit"] and any(a in ("-F-", "--file=-") or a in ("-F", "--file")
                                                               and b == "-" for a, b in zip(w, w[1:] + [""])))


def _words(text) -> list[str]:
    """shlex's words of `text`; one whose raw text starts with a redirection operator is a _Redir, judged before the
    quotes go (m2b.2 r3). Unbalanced quotes: split on whitespace, nothing marked, so nothing is taken out."""
    lex, out, pos = shlex.shlex(text, posix=True), [], 0
    lex.whitespace_split, lex.commenters = True, ""
    try:
        for w in lex:
            raw, pos = text[pos:lex.instream.tell()].lstrip(), lex.instream.tell()
            m = _REDIR.fullmatch(w)
            out.append(_Redir(w) if m and raw.startswith(w[:m.start(2)]) else w)
    except ValueError:  # unbalanced quotes
        return text.split()
    return out


def _skip(w, i, takes, long_takes=()) -> int:
    """Index of the first word from w[i] past a wrapper's options; `takes`: its short options that take a value."""
    while i < len(w) and w[i].startswith("-"):
        a, i = w[i], i + 1
        if a == "--":
            break
        if a.startswith("--"):
            i += "=" not in a and a in long_takes
        elif k := next((j for j, ch in enumerate(a[1:], 1) if ch in takes), None):
            i += k == len(a) - 1  # else the value is attached (-uroot)
    return i


def _getopt(w, i, short, long) -> tuple[list, int] | None:
    """([(option, value)], index of the first word past them) of a wrapper's options from w[i], read as getopt does up
    to the first other word: `short` its one-letter ones, `:` after one taking a value (attached, else the next word),
    `::` one taking only an attached value; `long` its long ones without `--`, `=` ending one taking a value (after
    `=`, else the next word), `?` one taking a value only after `=`, a unique prefix standing for one. None on an
    option it does not know or a value missing."""
    out = []
    while i < len(w) and w[i][:1] == "-" and w[i] != "-":
        a, i = w[i], i + 1
        if a == "--":
            break
        if a[:2] == "--":
            o, eq, v = a[2:].partition("=")
            names = [n for n in long if n.rstrip("=?") == o] or [n for n in long if n.startswith(o)]
            if len(names) != 1 or eq and names[0][-1] not in "=?":
                return None
            if names[0][-1] == "=" and not eq:
                if i == len(w):
                    return None
                v, i = w[i], i + 1
            out.append(("--" + names[0].rstrip("=?"), v))
            continue
        for j, c in enumerate(a[1:], 1):
            k = short.find(c)
            if k < 0 or c == ":":
                return None
            if short[k + 1:k + 2] != ":":
                out.append(("-" + c, ""))
                continue
            v = a[j + 1:]
            if not v and short[k + 2:k + 3] != ":":
                if i == len(w):
                    return None
                v, i = w[i], i + 1
            out.append(("-" + c, v))
            break
    return out, i


def _timeout(w, env):
    got = _getopt(w, 1, *_TIMEOUT)
    return w[got[1] + 1:] if got and got[1] < len(w) and _DURATION.fullmatch(w[got[1]]) else None


def _nice(w, env):
    i = 1
    while i < len(w) and re.fullmatch(r"-[+-]?\d+", w[i]):  # the old `nice -10`
        i += 1
    got = _getopt(w, i, *_NICE)
    return got and w[got[1]:]


def _xargs(w, env):
    """Its command with what it reads (_STDIN) added at the end and put for each replace-str (substrings too)."""
    if (got := _getopt(w, 1, *_XARGS)) is None:
        return None
    opts, i = got
    env.extend((v, None) for o, v in opts if o == "--process-slot-var")
    out = w[i:] or ["echo"]
    for r in (v or "{}" for o, v in opts if o in ("-I", "-J", "-i", "--replace")):
        out = [x.replace(r, _STDIN) for x in out]
    return [*out, _STDIN]


def _uv(w, env):
    """`uv [options] run [options] command`; `--module` runs python -m. Run puts the project's venv first on PATH, and
    an env file (--env-file, UV_ENV_FILE) may set anything."""
    got = _getopt(w, 1, *_UV)
    if got is None or w[got[1]:got[1] + 1] != ["run"] or (more := _getopt(w, got[1] + 1, *_UV)) is None:
        return None
    opts = {o for o, _ in got[0] + more[0]}
    env.append(("PATH", None))
    if "--env-file" in opts or any(n == "UV_ENV_FILE" for n, _ in env):
        env.append(("*", None))
    return ["python", "-m", *w[more[1]:]] if opts & {"-m", "--module"} else w[more[1]:]


def _env(w, env):
    """Its command, or `env` again with the words of its -S strings (which it reads as its own arguments)."""
    if (got := _getopt(w, 1, *_ENV)) is None:
        return None
    opts, i = got
    if w[i:i + 1] == ["-"]:  # as -i
        opts, i = [*opts, ("-i", "")], i + 1
    split = []
    for o, v in opts:
        if o in ("-i", "--ignore-environment"):
            env.append(("*", None))
        elif o in ("-u", "--unset"):
            env.append((v, None))
        elif o in ("-S", "--split-string"):
            if (words := _split_s(v)) is None:
                return None
            split += words
    return ["env", *split, *w[i:]] if split else w[i:]


_S_ESC = {"f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", **{c: c for c in "\"#$'\\"}}


def _split_s(s) -> list[str] | None:
    """The words env makes of its -S string `s` (coreutils env.c build_argv, FreeBSD's env alike), not a shell's
    (m2d.7 r1): `\\_` outside double quotes separates words (inside it is a space), `\\c` and a `#` starting a word
    end the string, in single quotes only `\\\\` and `\\'` escape; `${VAR}` stays as written, unexpanded shell syntax
    to the checks. None where env fails: an unknown escape, `\\c` in double quotes, an open quote."""
    out, cur, q, i = [], None, "", 0  # cur None: between words
    while i < len(s):
        c, i = s[i], i + 1
        if c in "'\"" and q in ("", c):
            q, cur = "" if q else c, cur or ""
        elif not q and (c in " \t\n\v\f\r" or s[i - 1:i + 1] == "\\_" or c == "#" and cur is None):
            i += c == "\\"
            if c == "#":
                break
            if cur is not None:
                out.append(cur)
            cur = None
        elif c == "\\" and (q != "'" or s[i:i + 1] in ("\\", "'")):
            n, i = s[i:i + 1], i + 1
            if n == "c" and not q:
                break
            if (ch := " " if n == "_" else _S_ESC.get(n)) is None:
                return None
            cur = (cur or "") + ch
        else:
            cur = (cur or "") + c
    return None if q else out + ([cur] if cur is not None else [])


_OPTS = {"timeout": _timeout, "nice": _nice, "xargs": _xargs, "uv": _uv, "env": _env}


def _wrapper(w):
    """_norm's reader of wrapper w[0] (_OPTS), else None; uv is one only with a word `run`."""
    h = _name(w[0])
    return _OPTS.get(h) if h != "uv" or "run" in w else None


def _stuck(w) -> bool:
    """`w` (_norm) still starts with a wrapper of _OPTS: _norm could not read its options."""
    return bool(f := _wrapper(w)) and f(list(w), []) is None


def _fold(x) -> str:
    """`x` case-folded on a case-insensitive volume, where `NPM` runs npm and `git PUSH` git-push (REQ-20, m2d.7 ③).
    ponytail: pathmatch.FOLD probes the volume holding foremind, not each PATH directory; a shell's own builtins
    (`EXEC`, `SOURCE`) stay case-sensitive yet are folded too, which only makes a check stricter."""
    return x.casefold() if pathmatch.FOLD else x


def _name(x) -> str:
    """Command word `x` as a name: its basename, zsh's `=cmd` as cmd, folded (_fold)."""
    return _fold(os.path.basename(x.lstrip("=")))


def _norm(w, env=None) -> list[str]:
    """Words of one simple command without its redirections wherever they stand, leading VAR=value, the wrappers
    running the next word (env with its -S split, sudo, _OPTS, _WRAPPERS, zsh's `repeat N`, their options skipped);
    the first word as a name (_name). Every check reads these words, so none takes `2>/dev/null foremind …` for a
    command `null` or stops at a `2>&1` before `-m`/`-c` (m2b.2 r2). When a redirection cannot be taken out
    (_unredirect), `w` as it is: _bash_matrix denies it; a wrapper of _OPTS whose options cannot be read stays the
    command (_stuck). `env`, when given, gets (name, value) of what the command runs with, in order: VAR=value (a
    word in its place a wrapper would fail to run, no harm), env's -u with None, ("*", None) where anything may
    change (env -i, sudo); _xargs, _uv add theirs."""
    if (u := _unredirect(w)) is None:
        return list(w)
    w, env = u, [] if env is None else env
    while w:
        h = _name(w[0])
        if _ASSIGN.fullmatch(w[0]):
            n, _, v = w[0].partition("=")
            env.append((n.rstrip("+"), None if n.endswith("+") else v))
            w = w[1:]
        elif h == "sudo":  # nothing after the options: -s/-i open a shell that reads stdin
            env.append(("*", None))
            w = w[_skip(w, 1, "ugpCDRrTtU", _SUDO_LONG):] or ["sh"]
        elif h == "repeat":
            w = w[2:]
        elif h in _WRAPPERS:
            w = w[_skip(w, 1, "a"):]
        elif f := _wrapper(w):
            if (r := f(w, env)) is None:
                return [h, *w[1:]]
            w = r
        else:
            return [h, *w[1:]]
    return []


def _inner(w) -> str | None:
    """The command text `bash|sh|zsh -c '<text>'` or `eval <text>` runs, else None."""
    if w[0] == "eval":
        return " ".join(w[1:])
    if w[0] not in _SHELLS:
        return None
    i, c = 1, False
    while i < len(w) and w[i][:1] in ("-", "+") and w[i] not in ("-", "--", "+"):
        a, i = w[i], i + 1
        if a.startswith("--"):
            i += a in ("--rcfile", "--init-file")
        else:
            c, i = c or "c" in a, i + (a[-1] in "oO")
    i += w[i:i + 1] == ["--"]
    return w[i] if c and i < len(w) else None


def _parse(cmd, code, raw=None, outer=()) -> list[tuple[list[str], str]]:
    out, bodies, seen = [], [], [*outer]  # seen: outer and _pre of each command so far here
    texts = [(t, raw, outer) for t in _scan(cmd, code=code, bodies=bodies)[0]]
    whole = raw or " ".join(_words(cmd))  # a body's reader may be past a pipe (`cat <<EOF | sudo sh`)
    for text, r, o in texts + [(t, whole, None) for _, _, run in bodies for t in run]:
        ww, env = _words(text), []
        if w := _norm(ww, env):
            w = _Cmd(w)
            w.own, w.top, w.env, r = ww, r is None, env, r or " ".join(ww)
            # a body's reader may be any command before it here, an earlier line of it too (`sudo -s` reads the rest)
            w.outer = tuple(seen) if o is None else o
            new = [(w, r), *(_parse(t, code, r, [*w.outer, *_pre(w)]) if (t := _inner(w)) is not None else [])]
            seen += (x for c, _ in new for x in _pre(c))
            out += new
    if not code and (any(_name(x) in _SHELLS or x == "eval" for w, _ in out for x in w)
                     or any(w[0] in ("source", ".") for w, _ in out)):
        return _parse(cmd, True, raw, outer)  # a shell may read the heredoc bodies (`bash <<EOF`, `cat <<EOF | sh`)
    return out


def _commands(cmd) -> list[tuple[list[str], str]] | None:
    """(normalized words (_norm), raw text) of each simple command in `cmd`, plus of the text a shell -c or eval runs.
    Raw: the words as written, quotes gone and whitespace one space, VAR=value, env, sudo and a path kept — what an
    exemption must name (m2a.5.F1); text a shell -c or eval runs gets the raw text of the command that runs it, a
    heredoc body a shell reads that of the whole command (`sudo -s <<EOF …` keeps the sudo, m2b.2 r1).
    None when parsing fails (nesting past the recursion limit, or a slip): the whole command is then denied.
    ponytail: other wrappers (find -exec, parallel), functions, aliases and variables are not unwrapped, and a Bash
    write is not tied to a path; the gate's diff and L0 catch those afterwards."""
    try:
        return _parse(cmd, False)
    except Exception:  # noqa: BLE001 — a parser slip must not open a hole
        return None


def _bare(cmd) -> str:
    """`cmd` without its heredoc bodies (delimiter lines included)."""
    bodies = []
    _scan(cmd, bodies=bodies)
    for start, end, _ in reversed(bodies):
        cmd = cmd[:start] + cmd[end:]
    return cmd


def _groups(bare, cmds) -> list[tuple[list[str], list[str], list[str]]]:
    """_match's command groups ([segment], [raw text], words an `unless` must clear): the whole command without its
    heredoc bodies (`bare`; a body's commands are groups of their own; an interpreter's is no command, m2b.2 r1), with
    the words of its first simple command (never re-split from the joined text: quotes are gone there), and each
    simple command. A segment has no redirections (`npm 2>&1 install x` is `npm install x`, r1, _norm): unless one
    cannot be taken out, when it keeps them and _could fails closed on them."""
    ww = _words(bare)
    out = [(whole, [" ".join(ww)], cmds[0][0] if cmds else whole)] if (whole := _norm(ww)) else []
    return [([" ".join(w)], r, u) for w, r, u in out + [(w, [r], w) for w, r in cmds]]


# The foremind a Foremind session may run, by FOREMIND_ROLE (I58, m2b.2): a whitelist, so a command added later is
# denied until listed here; an unknown role gets the None row alone. _ANY: subcommands with any arguments. No `hook`:
# Claude Code runs the hooks itself, never through Bash (a forged PostToolUse stdin could write user_config_edit).
_ANY = {None: {"version", "status", "report", "ask", "handoff", "release-check", "statusline"},
        "seat": {"gate"}, "controller": {"run", "say"}}
_ALLOWED = {None: ("version、status、report、ask、handoff、release-check、statusline；不带 --author 的 log；decide 只允许"
                   "无参数、show [Q-n]、--new …、--notify <字面 Q-n>；plan show、plan validate；不带 --reset 的 quota；"
                   "不带 --rescan 的 doctor"),
            "seat": "review [批次]（不带选项）；gate", "controller":
            "review --request-changes；不带 --user-approved 的 plan amend；run；say", "planner": "plan submit"}
# argparse options (name: takes a value) of the subcommands allowed only in some forms
_DECIDE = {"--note": 1, "--void": 1, "--reason": 1, "--new": 0, "--question": 1, "--option": 1, "--recommended": 1,
           "--category": 1, "--irreversible": 0, "--path": 1, "--command": 1, "--approve-option": 1, "--blocks": 1,
           "--notify": 1}
_NEW = {"--new", "--question", "--option", "--recommended", "--reason", "--category", "--irreversible", "--path",
        "--command", "--approve-option", "--blocks"}
_REVIEW = {"--run-reviewer": 0, "--request-changes": 0, "--item": 1}
_AMEND = {"--reason": 1, "--batch": 1, "--drop": 1, "--goal": 1, "--user-approved": 0}
_QUOTA = {"--reset": 0, "--group": 1}
_LOG = {"--author": 1, "--file": 1}
_DOCTOR = {"--rescan": 0, "--yes": 0}


def _opts(args, spec) -> tuple[list, list] | None:
    """([(option, index of its word, value)], positionals) of `args` as argparse reads them against `spec`: an exact
    name, else a unique prefix of a long one (`--req`); the value after `=` or in the next word unless that starts with
    `-`. None on any other word starting with `-` (unknown, ambiguous, single-dash, `--`)."""
    opts, pos, i = [], [], 0
    while i < len(args):
        a, k, i = args[i], i, i + 1
        if a[:1] != "-" or a == "-":
            pos.append(a)
            continue
        o, eq, val = a.partition("=")
        names = [n for n in spec if n == o] or [n for n in spec if len(o) > 2 and o[:2] == "--" and n.startswith(o)]
        if len(names) != 1:
            return None
        if spec[names[0]] and not eq:
            val = args[i] if i < len(args) and args[i][:1] != "-" else None
            i += val is not None
        opts.append((names[0], k, val))
    return opts, pos


def _without(args, spec, flag) -> bool:
    """`args` parse against `spec` without `flag`, and no word is left with shell syntax (it may expand to `flag`)."""
    got = _opts(args, spec)
    return got is not None and all(n != flag for n, _, _ in got[0]) and not any(map(_SPECIAL.search, args))


def _with(args, spec, flag) -> int | None:
    """Index of `flag`'s word when `args` parse against `spec` and every word before it is literal (no expansion there
    can end the options or add a `--` before it), else None."""
    got = _opts(args, spec)
    k = next((k for n, k, _ in got[0] if n == flag), None) if got else None
    return None if k is None or any(map(_SPECIAL.search, args[:k])) else k


def _allowed(args, role) -> bool:
    sub, rest = args[0], args[1:]
    if sub in _ANY[None] or sub in _ANY.get(role, ()):
        return True
    if sub == "plan":
        return (rest[:1] in (["show"], ["validate"]) or rest[:1] == ["submit"] and role == "planner"
                or rest[:1] == ["amend"] and role == "controller" and _without(rest[1:], _AMEND, "--user-approved"))
    if sub == "quota":
        return _without(rest, _QUOTA, "--reset")
    if sub == "doctor":
        return _without(rest, _DOCTOR, "--rescan")
    if sub == "log":  # --author could sign as the user
        return _without(rest, _LOG, "--author")
    if sub == "review":  # a seat's is `review [batch]`: --run-reviewer is the supervisor's (commands/review.py)
        return (role == "seat" and len(rest) <= 1 and not any(a[:1] == "-" or _SPECIAL.search(a) for a in rest)
                or role == "controller" and _with(rest, _REVIEW, "--request-changes") is not None)
    if sub != "decide":
        return False
    if not rest or rest[0] == "show":
        return len(rest) <= 1 or len(rest) == 2 and bool(re.fullmatch(Q_ID, rest[1]))
    got = _opts(rest, _DECIDE)
    if got is None or got[1]:  # decide takes no positionals besides show's and an answer's
        return False
    if _with(rest, _DECIDE, "--new") is not None:  # decide runs --new before anything else
        return all(n in _NEW for n, _, _ in got[0])
    return len(got[0]) == 1 and got[0][0][0] == "--notify" and bool(re.fullmatch(Q_ID, got[0][0][2] or ""))


def _session_denied(w, role) -> bool:
    """`foremind …` (or `python* -m foremind …`) outside `role`'s whitelist (_ANY, _allowed). Fail-closed: the
    subcommand must be literal and a lone `--` denies (argparse may skip it before a subcommand); the forms allowed
    only without an option deny on any word left with shell syntax, those allowed with one (decide --new, review
    --request-changes) on such a word before it; `decide show` and `--notify` take a literal Q-n. FOREMIND_SESSION is
    trivially unset inside a command, so every Foremind session is held to this."""
    args = _foremind_args(w)
    if args is None:
        return False
    return not args or "--" in args or bool(_SPECIAL.search(args[0])) or not _allowed(args, role)


def _foremind_args(w, flags=None, code=None) -> list | None:
    """The words after `foremind` in `w` (_norm) running it, else None. `flags`: a set that gets the one-letter options
    before `-m`/`-c` of a python* (`-P -m`, `-Pm`); `code`: a list that gets the text of its -c. What xargs adds
    (_STDIN) where python* reads an option or its program may be `-m foremind`: the words after it."""
    if w[0] == "foremind":
        return w[1:]
    if not _PYTHON.fullmatch(w[0]):
        return None
    i = 1
    while i < len(w):
        a, i = w[i], i + 1
        if _STDIN in a:
            return w[i:]
        if not a.startswith("-") or a == "-":
            return None
        if a.startswith("--"):
            i += a == "--check-hash-based-pycs"
            continue
        k = next((j for j, ch in enumerate(a[1:], 1) if ch in "cmWX"), None)
        if flags is not None:
            flags.update(a[1:k])
        if k is None:
            continue
        val = a[k + 1:] or (w[i] if i < len(w) else "")
        i += not a[k + 1:]
        if a[k] == "c":
            if code is not None:
                code.append(val)
            return None
        if a[k] == "m":
            return w[i:] if val.partition(".")[0] == "foremind" or _SPECIAL.search(val) else None
    return None


def _push(w, loose=True) -> str | None:
    """The #23 rule `git push` / `gh pr create` matches, else None; with `loose` a word there left with shell syntax
    matches too."""

    def at(i, *lits):
        return i < len(w) and (w[i] in lits or loose and bool(_SPECIAL.search(w[i])))
    if w[0] == "gh":
        return "gh pr create*" if at(1, "pr") and (at(2, "create", "new") or loose and _SPECIAL.search(w[1])) else None
    return "git push*" if _git_sub(w, "push", loose=loose) else None


def _git_sub(w, *subs, loose=True) -> bool:
    """`w` runs git subcommand `subs` (the options before it skipped); with `loose` a word there left with shell
    syntax matches too; folded (_fold)."""
    if w[0] != "git":
        return False
    i = _git_at(w)
    return i < len(w) and (_fold(w[i]) in subs or loose and bool(_SPECIAL.search(w[i])))


def _git_at(w) -> int:
    """Index of git's subcommand word in `w` (its options skipped)."""
    i = 1
    while i < len(w) and w[i].startswith("-"):
        i += 1 + (w[i] in _GIT_ARG)
    return i


def _renamed(w) -> bool:
    """git given config that could run a push or --output under another name (r2): `-c` with a _GIT_HIDE key or a
    value left with shell syntax, any --config-env, `git config` naming such a key; `gh alias set|import` likewise."""

    def hides(v):
        return v.lower().startswith(_GIT_HIDE) or bool(_SPECIAL.search(v))
    if w[0] == "gh":
        return w[1:2] == ["alias"] and len(w) > 2 and (w[2] in ("set", "import") or bool(_SPECIAL.search(w[2])))
    if w[0] != "git":
        return False
    i = 1
    while i < len(w) and w[i].startswith("-"):
        a = w[i]
        if a.startswith("--config-env") or a[:2] == "-c" and hides(a[2:] or "".join(w[i + 1:i + 2])):
            return True
        i += 1 + (a in _GIT_ARG)
    return i < len(w) and _fold(w[i]) == "config" and any(map(hides, w[i + 1:]))


def _push_by_user(cfg, header) -> bool:
    """#23 is the user's for some repo of the batch (unset = user, §7.5); an unreadable header counts as that too."""
    rids = _strs((header or {}).get("repos"))
    return header is None or not rids or not all(review.push_by_system(cfg, r) for r in rids)


# words that run the next command with FOREMIND_SESSION changed or gone: env (-i, -u, VAR=), sudo (env_reset), exec -c
_REENV = re.compile(r"(?:^| )=?(?:\S*/)?(?:env|sudo|exec)(?: |$)", re.I)


def _pre(w) -> list[str]:
    """`w`'s (_Cmd) own words before its command word: the first own word named w[0] whose following words, redirections
    taken out (_unredirect), are w's rest word for word (xargs adds _STDIN at the end; a rest word with _STDIN in it,
    xargs -I's, is any word, r2), so that a redirection target or an option's value of that name is none
    (`>/tmp/bash sudo bash`, `xargs -a /tmp/bash sudo bash`, r1). All of them when no word is (_norm made it:
    `sudo -s` runs sh, `uv run --module` python)."""
    rest = _unredirect(w[1:])
    for k, x in enumerate(w.own):
        if (not _ASSIGN.fullmatch(x) and _name(x) == w[0] and rest is not None
                and (s := _unredirect(w.own[k + 1:])) is not None
                and any(len(t) == len(rest) and all(r == y or _STDIN in r for r, y in zip(rest, t))
                        for t in (s, [*s, _STDIN]))):
            return w.own[:k]
    return w.own


def _reenv(w, raw) -> bool:
    """_REENV runs `w` (_Cmd): among the words before its command word (_pre), its own and those of each command
    around it (_Cmd.outer: bash -c, eval, a heredoc a shell reads, m2b.2 r2). Free text after a command word is no
    wrapper (REQ-20; nested, m2e REQ-13). A nested command whose command word is none of its own words: anywhere in
    `raw`, as before."""
    pre = _pre(w)
    if not w.top and len(pre) == len(w.own):
        return bool(_REENV.search(raw))
    return bool(_REENV.search(" ".join([*w.outer, *pre])))


def _off_main(w) -> bool:
    """`w` (_Cmd) is a `python* -m foremind` off the main checkout (_pinned): it may import foremind from its cwd, in a
    worktree the batch branch's code (finding 28, REQ-2); or a `foremind …` run with PATH changed (uv run puts the
    project's venv first). Only what the command itself runs with (_Cmd.env): under bash -c or a heredoc commands
    share their raw text, and an assignment on the enclosing command does not count either."""
    flags = set()
    if _foremind_args(w, flags) is None:
        return False
    if w[0] == "foremind":
        return any(n in ("PATH", "*") for n, _ in w.env)
    return not _pinned(w, flags)


def _pinned(w, flags) -> bool:
    """REQ-2's prefix: `-P` among the interpreter's one-letter `flags`, and the PYTHONPATH `w` (_Cmd) runs with, the
    last in its env (_norm: env -i, sudo and the like leave it unknown), this package's parent. A relative one: the
    command's cwd, not the hook's, resolves it."""
    pp = [v for n, v in w.env if n in ("PYTHONPATH", "*")]
    return ("P" in flags and bool(pp) and pp[-1] is not None and os.path.isabs(pp[-1])
            and os.path.realpath(pp[-1]) == os.path.realpath(claude._PKG_PARENT))


def _bash_matrix(w, role, resign=False) -> str | None:
    """`resign`: the Bash command names FOREMIND_SESSION somewhere or runs this one under _REENV (m2b.2 r2)."""
    seg = " ".join(w)
    if _SPECIAL.search(w[0]):
        return (f"`{seg}`：命令名里有未展开的 shell 写法（$、反引号、通配符、花括号列表、重定向），看不出是什么命令，"
                "按拦截处理；把命令名写成字面再执行（heredoc 正文只在 cat、tee、foremind log/handoff、git commit -F -、"
                "python*/node/ruby/perl 读时当数据，其他命令读的正文逐行按命令判）")
    if _stuck(w) and any(map(_FM.search, w[1:])):
        return (f"`{seg}`：包装命令（timeout、nice、xargs、uv run、env）的选项认不出，看不出它实际执行什么，而其中出现 "
                "foremind 或 python，按拦截处理（不可豁免，不生成待决）；去掉包装、或只用它常见的选项再执行")
    if _session_denied(w, role):
        extra = f"；角色 {role} 另可：{_ALLOWED[role]}" if role and role in _ALLOWED else ""
        return (f"`{seg}`：Foremind 会话按角色白名单执行 foremind 命令，这个用法不在其中（不可豁免，不生成待决）。"
                f"所有角色可：{_ALLOWED[None]}{extra}。只有用户执行的命令写进批次日志请用户在自己的终端里执行；"
                "foremind 命令里有未展开的 shell 写法（$、反引号、通配符、花括号列表）或单独的 `--` 时也按此拒，"
                "写成字面、去掉 `--` 再执行")
    if _unredirect(w) is None:
        return (f"`{seg}`：单独的 `<`、`>` 类重定向后面没有目标或跟着以 - 开头的词，引号去掉后看不出是重定向还是参数，"
                "看不清这条命令，按拦截处理；重定向写上目标，参数里的 `<`、`>` 与相邻文字写成一个词再执行")
    if resign and _foremind_args(w) is not None:
        return (f"`{seg}`：Foremind 会话执行 foremind 命令时，同一条 Bash 命令不能改写 FOREMIND_SESSION（VAR=、env、"
                "unset、export、sudo、exec，命令里出现这个名字即按改写处理）：foremind 按它认作者与锁持有者，改写等于"
                "冒名（不可豁免，不生成待决）；去掉这些再执行，要看它的值请单独执行")
    if _off_main(w):
        return (f"`{seg}`：Foremind 会话的 foremind 命令固定走主检出代码：`python* -m foremind` 要在解释器前写 "
                "`PYTHONPATH=<主检出>` 并带 `-P`，否则会从当前目录（worktree）导入本批分支代码；`foremind` 也不经 uv run "
                "或改写的 PATH 找（不可豁免，不生成待决）；"
                f"整行前缀用 `{claude.foremind_command()}` 再执行")
    flags, code = set(), []
    if (_foremind_args(w, flags, code) is None and code and (re.search(r"\bforemind\b", code[0]) or _STDIN in code[0])
            and not _pinned(w, flags)):  # code xargs reads may be foremind's, as its -m (m2d.7 r1 note)
        return (f"`{seg}`：`python* -c` 的代码用到 foremind 时同样固定走主检出代码：解释器前要写 `PYTHONPATH=<主检出>` "
                "并带 `-P`，否则会从当前目录（worktree）导入本批分支代码（不可豁免，不生成待决）；写成 "
                f"`PYTHONPATH={shlex.quote(str(claude._PKG_PARENT))} python3 -P -c …` 再执行")
    if role == "planner" and _git_sub(w, "add", "commit"):
        return (f"`{seg}`：规划者不提交（git add、git commit 不用）：草稿只写在草稿目录，"
                "由 foremind plan submit 写进计划")
    if _renamed(w):
        return (f"`{seg}`：git 的 -c alias.*/include.*、--config-env、git config 设别名或 include（gh alias set/import "
                "同理）能让 push、--output 换个名字执行，看不出实际执行的是什么，按拦截处理；直接写子命令，"
                "要改别名请用户在自己的终端里做")
    if w[0] == "git" and any(a.startswith("--") and (_SPECIAL.search(o := a.partition("=")[0]) or len(o) > 2 and
                                                     "--output".startswith(o)) for a in w[1:]):
        return (f"`{seg}`：git 的 --output 能覆盖任意文件，不用；要存输出就重定向到本批 owns_paths 内的文件"
                "（`git … > <文件>`）")
    return None


def system_glob(path) -> str | None:
    """The #22 glob `path` (absolute; realpath'd, or a link's own, bounds.outside) falls under, else None."""
    globs = (*SYSTEM_GLOBS, os.path.realpath(user_config_dir()).lower() + "/*")
    return next((g for g in globs if fnmatchcase(path.lower(), g)), None)


def edit_path(tool, tool_input, cwd) -> str | None:
    """The absolute path an Edit/Write-class tool writes, else None."""
    return _abs(tool_input.get(EDIT_TOOLS[tool]), cwd) if tool in EDIT_TOOLS else None


def _abs(p, cwd) -> str | None:
    if not isinstance(p, str) or not p:
        return None
    return os.path.realpath(os.path.join(os.path.expanduser(cwd or "/"), os.path.expanduser(p)))


def _rel(path, base) -> str | None:
    base = os.path.realpath(base).rstrip("/") + "/"
    return path[len(base):] if path.startswith(base) else None


def locate(path, root, batch, repos, cfg=None) -> tuple[str, str | None]:
    """("worktree", "<repo>:<path>") inside this batch's worktrees; else "other_worktree" (also the batch dir's own
    files and `_`-dirs such as `_ro/`: no repo id starts with `_`, I28), ("repo", qualified) for a main checkout,
    "project", or "outside"."""
    if batch and (rel := _rel(path, worktree.batch_dir(project_slug(root, cfg or {}), batch))) is not None:
        repo, _, sub = rel.partition("/")
        return ("worktree", f"{repo}:{sub}") if sub and not repo.startswith("_") else ("other_worktree", None)
    if _rel(path, worktree.wt_root()) is not None:
        return "other_worktree", None
    for r in repos:
        if (rel := _rel(path, r.path)) is not None:
            return "repo", f"{r.id}:{rel}"
    return ("project" if _rel(path, root) is not None else "outside"), None


def _strs(v) -> list:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def active_patterns(cfg: dict) -> list[dict]:
    """Built-in patterns, plus configured `hard_block.patterns` entries ({category, paths?, commands?, tools?}) whose
    category is a built-in one or listed in `hard_block.categories` (a batch header's hard_block adds to it)."""
    cats, pats = cfg.get("hard_block.categories"), cfg.get("hard_block.patterns")
    on = {p["category"] for p in DEFAULT_PATTERNS} | {c for c in (cats if isinstance(cats, list) else ())
                                                      if isinstance(c, int)}
    extra = [p for p in (pats if isinstance(pats, list) else ())
             if isinstance(p, dict) and isinstance(p.get("category"), int) and p["category"] in on]
    return [*DEFAULT_PATTERNS, *extra]


def _could(seg, pat) -> str | None:
    """From a word of `seg` left with shell syntax (_SPECIAL) on, the command could be anything: it hits `pat` when the
    words before it do (the rest filled in from `pat`); that word then, else None. A command word like that is the
    matrix's (_bash_matrix). `seg` comes without its redirections (_norm): `python3 <<EOF` is no
    `python3 -m pip install`.
    ponytail: `[…]` classes in the rest of `pat` are not filled in; a configured pattern using them may miss here."""
    w = seg.split(" ")
    k = next((i for i, x in enumerate(w) if _SPECIAL.search(x)), 0)
    return w[k] if k > 0 and fnmatchcase(" ".join(w[:k] + [x.replace("*", "") for x in pat.split(" ")[k:]]), pat) \
        else None


def _match(patterns, kind, groups) -> list[Hit]:
    """groups: (forms matched against the patterns, the values a hit carries, words an `unless` clears or None).
    A pattern no form matches as written but one could through shell syntax (_could) gives a Hit with `shell` set."""
    hits, seen = [], set()
    for forms, values, words in groups:
        for p in patterns:
            pats, shell = _strs(p.get(kind)), None
            # a form's command word is a _name: fold the pattern's the same way (`R CMD INSTALL *`, REQ-20, r1)
            fold = {x: (h.casefold() if pathmatch.FOLD and kind == "commands" else h) + sp + t
                    for x in pats for h, sp, t in [x.partition(" ")]}
            pat = next((x for x in pats if any(fnmatchcase(v, fold[x]) for v in forms)), None)
            if not pat and kind == "commands":
                pat, shell = next(((x, s) for x in pats for v in forms if (s := _could(v, fold[x]))), (None, None))
            if pat and words is not None and callable(u := p.get("unless")) and u(words):
                continue
            if pat and (key := (tuple(values), p["category"], shell is None)) not in seen:  # as pushes
                seen.add(key)
                hits.append(Hit(p["category"], kind, list(values), pat, shell))
    return hits


def _matrix(path, where, qual, *, root, session, batch, role, header) -> list[str]:
    if where == "outside":  # N-6: a seat's only place outside the project is the system temp dir
        tmp = list(dict.fromkeys(os.path.realpath(d) for d in (tempfile.gettempdir(), "/tmp", "/private/tmp")))
        if not batch or role not in (None, "seat") or any(_rel(path, d) is not None for d in tmp):
            return []
        return [f"{path} 在项目之外；席位只写本批 worktree 里的 owns_paths 与系统临时目录（{'、'.join(tmp)}），"
                "其余路径不写（可写矩阵 §2.3）；临时文件放临时目录"]
    if not batch or role not in (None, "seat"):
        who = f"角色 {role}" if role not in (None, "seat") else "没有批次的会话"
        return [f"{path}：{who}不写 worktree 与产品代码（可写矩阵 §2.3）；改动交给席位或经对应的 foremind 命令"]
    if where != "worktree":
        return [f"{path} 不在本批 {batch} 的 worktree 内；席位只写本批 worktree 里的 owns_paths"]
    out = []
    owns = _strs((header or {}).get("owns_paths"))
    if header is not None and not (qual and pathmatch.owns(qual, owns)):
        out.append(f"{qual or path} 不在本批 owns_paths（{'、'.join(owns) or '空'}）内；需要写就执行 "
                   "`foremind decide --new` 申请扩大范围（#8），获批前不要绕路")
    if (holder := lock_holder(root, batch)) != session:
        out.append(f"批次 {batch} 的锁持有者是 {holder or '（无）'}，不是本会话 {session}；只有持锁会话能写，"
                   "接手经 `foremind handoff --accept`")
    return out


def evaluate(tool, tool_input, cwd, *, root=None, session=None, batch=None, role=None, header=None, cfg=None,
             repos=(), now=None) -> Verdict:
    """`session` None = not a Foremind session. `header` None = batch header unreadable: the owns_paths check is
    skipped and no exemption applies (I38); pass {} for sessions without a batch."""
    v, hits = Verdict(session is not None), []
    path = edit_path(tool, tool_input, cwd)
    where, qual = locate(path, root, batch, repos, cfg) if path and root else ("outside", None)
    state = bool(path) and fnmatchcase(path.lower(), STATE_GLOB)
    forms = [path, qual] if qual else [path]
    if path and (g := system_glob(path)):
        state = False
        if v.foremind:
            hits.append(Hit(22, "paths", forms, g))
        elif g == DELIVERY_GLOB:
            v.matrix = [DELIVERY_REASON.format(path=path)]
    if v.foremind:
        pats = active_patterns(cfg or {})
        if path:
            hits += _match(pats, "paths", [(forms, forms, None)])
        if tool == "Bash":
            cmd = str(tool_input.get("command") or "")
            cmds = _commands(cmd)
            # the text outside heredoc bodies, and each command as run (a body a shell reads carries the whole command)
            texts = [] if cmds is None else [_bare(cmd), *(r for _, r in cmds)]
            if cmds is None:
                v.matrix = [DEEP_REASON]
            elif any(p in t for t in texts for p in ("<(", ">(")):
                v.matrix = [PROC_REASON]
            else:
                hits += _match(pats, "commands", _groups(texts[0], cmds))
                if batch and _push_by_user(cfg or {}, header):  # N-12: only a batch's pushes are its to ask for
                    # matched only through a word left with shell syntax: that word (a matrix denial, REQ-1); keyed
                    # apart from a literal push, since commands under one bash -c or heredoc share their raw text
                    pushes = {}
                    for w, raw in cmds:
                        if pat := _push(w):
                            # the word in the subcommand's place, not an option's value (`git -C $D $X`: `$X`, r3)
                            at = w[1:3] if w[0] == "gh" else w[_git_at(w):]
                            shell = None if _push(w, False) else next(x for x in at if _SPECIAL.search(x))
                            pushes.setdefault((raw, shell is None), Hit(23, "commands", [raw], pat, shell))
                    hits += pushes.values()
                # bare too: an assignment on its own (`FOREMIND_SESSION=x; foremind log`) is no command (r3)
                resign = any("FOREMIND_SESSION" in t for t in texts)
                v.matrix = [r for w, raw in cmds
                            if (r := _bash_matrix(w, role, resign or _reenv(w, raw)))]
        tool_hits = _match(pats, "tools", [([tool], [tool], None)])
        if tool_hits and (allow := allowed_tools()):
            tool_hits = [h for h in tool_hits if h.category != 20 or not any(fnmatchcase(tool, g) for g in allow)]
        hits += tool_hits
    v.matrix += [h.reason() for h in hits if h.shell]
    for h in (h for h in hits if not h.shell):
        q = (exemptions.find(root, batch, h.kind, h.values, category=h.category, header=header, now=now)
             if v.foremind and root and header is not None else None)  # unreadable header: maybe delivered
        if q:
            v.exempted.append((q, h))
        else:
            v.pending.append(h)
    if state:
        v.matrix = [STATE_REASON.format(path=path)]
    elif v.foremind and path:
        v.matrix = _matrix(path, where, qual, root=root, session=session, batch=batch, role=role, header=header)
    return v
