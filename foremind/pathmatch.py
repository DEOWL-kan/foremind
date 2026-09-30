"""One way to compare repo-qualified paths `<repo>:<path>` and their globs (REQ-19; DESIGN §20 I8, I11).

Globs are fnmatch (`*` crosses `/`); a pattern ending in `/` owns everything below it. Before comparing, both
sides go through norm() and, on a case-insensitive volume, casefold: `main:./Foremind//x.py` is `main:foremind/x.py`.
"""
import os
import re
from fnmatch import fnmatchcase

_WILD = re.compile(r"[*?[]")
_CLASS = re.compile(r"\[!?\]?[^\]]*\]")


def _case_insensitive() -> bool:
    p = os.path.abspath(__file__)
    try:
        return os.path.samefile(p, p.swapcase())
    except OSError:
        return False


# ponytail: probed once, on the volume holding this package (the main checkout), not per repo: a repo on a volume
# with the other case rule gets this one's. Probe each repo root (repos.py) if projects ever span such volumes.
FOLD = _case_insensitive()


def norm(qpath: str) -> str:
    """`qpath` without `.` segments and with `//` merged; a leading or trailing `/` stays."""
    repo, sep, p = qpath.partition(":") if ":" in qpath.split("/", 1)[0] else ("", "", qpath)
    body = "/".join(x for x in p.split("/") if x not in ("", "."))
    return repo + sep + "/" * p.startswith("/") + body + "/" * (p.endswith("/") and bool(body))


def _key(q: str) -> str:
    q = norm(q)
    return q.casefold() if FOLD else q


def _dir(p: str) -> str:
    return p + "*" if p.endswith("/") else p


def owns(qpath: str, patterns) -> bool:
    """Does one of `patterns` match `qpath`? A pattern ending in `/` matches everything below it."""
    q = _key(qpath)
    return any(fnmatchcase(q, _dir(_key(p))) for p in patterns)


def _meet(x: str, y: str) -> bool:
    if fnmatchcase(x, y) or fnmatchcase(y, x):
        return True
    if not _WILD.search(x) or not _WILD.search(y):
        return False  # a literal path the other pattern does not match
    # ponytail: two globs overlap unless their literal prefixes or suffixes rule it out; write an exact glob
    # intersection if the false positives serialise too much
    (x0, *_, x1), (y0, *_, y1) = (_WILD.split(_CLASS.sub("?", p)) for p in (x, y))  # [...] is one character
    return (x0.startswith(y0) or y0.startswith(x0)) and (x1.endswith(y1) or y1.endswith(x1))


def overlap(a: str, b: str) -> bool:
    """Could `<repo>:<glob>` a and b own (owns) a common file? Errs towards yes. Two literal paths also clash when one
    is below the other: `doc/x` as a file and `doc/x/y.md` cannot both be written."""
    (ra, _, pa), (rb, _, pb) = _key(a).partition(":"), _key(b).partition(":")
    if ra != rb:
        return False
    if not _WILD.search(pa + pb):
        x, y = pa.rstrip("/") + "/", pb.rstrip("/") + "/"
        return x.startswith(y) or y.startswith(x)
    return _meet(_dir(pa), _dir(pb))
