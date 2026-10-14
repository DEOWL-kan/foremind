"""REQ-1: the wheel and the sdist carry the ten packaged templates, LICENSE and one version, and nothing private.

build() is the offline build m3a.10's install smoke reuses (REQ-2): a temporary copy of the source, the declared
backend checked first (uv offline, else an interpreter with pip and a setuptools.build_meta that satisfies the
declaration), then the candidate. Only a missing tool (ToolMissing) may skip a plain local run; with
FOREMIND_REQUIRE_BUILD=1 it fails. A candidate that does not build is a failure, never a skip.
"""
import contextlib
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import foremind
from foremind.cli import main

ROOT = Path(__file__).resolve().parent.parent
REQUIRE = os.environ.get("FOREMIND_REQUIRE_BUILD") == "1"
# never in an artifact (REQ-1): the author's local records and install state
PRIVATE = ("SPEC.md", "PROGRESS.md", "handoff/", "review/", ".foremind/")
# uv's words for an offline cache without the backend: the one uv failure that means a missing tool
_NO_CACHE = re.compile(r"not found in the cache|wasn't found in the cache|network connectivity is disabled", re.I)
_TIMEOUT_S = 600


class ToolMissing(Exception):
    """No offline way to run the declared build backend."""


class BuildFailed(AssertionError):
    """The candidate source did not build (or the tool failed for another reason): a failure, never a skip."""


def build_env() -> dict:
    """The build's environment: the real HOME and any UV_CACHE_DIR stay, so the offline caches are found; no session
    marks and no PYTHONPATH pointing at a source tree."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("FOREMIND_") and k != "PYTHONPATH"}
    return {**env, "UV_OFFLINE": "1", "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}


def _run(argv, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, env=build_env(), capture_output=True, text=True, timeout=_TIMEOUT_S)


def build_system(src=ROOT) -> dict:
    return tomllib.loads(Path(src, "pyproject.toml").read_text(encoding="utf-8"))["build-system"]


def _satisfies(version, req) -> bool:
    """`name>=x.y` requirements only. ponytail: what pyproject declares today; a richer specifier needs a parser."""
    m = re.fullmatch(r"\s*[A-Za-z0-9_.-]+\s*>=\s*([0-9]+(?:\.[0-9]+)*)\s*", req)
    nums = lambda v: tuple(int(x) for x in re.findall(r"[0-9]+", v)[:3])
    return bool(m) and nums(version) >= nums(m[1])


def _probe(tmp, bs) -> Path:
    """A one-module project with the same build-system: building it shows the backend is there, apart from the
    candidate."""
    d = Path(tmp, "probe")
    (d / "probe_pkg").mkdir(parents=True)
    (d / "probe_pkg" / "__init__.py").write_text("")
    reqs = ", ".join(f'"{r}"' for r in bs["requires"])
    (d / "pyproject.toml").write_text(f'[build-system]\nrequires = [{reqs}]\nbuild-backend = "{bs["build-backend"]}"\n\n'
                                      '[project]\nname = "probe"\nversion = "0"\n', encoding="utf-8")
    return d


def preflight(tmp, src=ROOT) -> tuple[str, str | None]:
    """("uv", None) or ("pip", python): how to build offline. ToolMissing when neither can."""
    bs = build_system(src)
    probe, why = _probe(tmp, bs), []
    if shutil.which("uv"):
        r = _run(["uv", "build", "--offline", "--wheel", "--out-dir", str(Path(tmp, "probe-out")), str(probe)])
        if r.returncode == 0:
            return "uv", None
        if not _NO_CACHE.search(r.stderr):
            raise BuildFailed(f"uv could not build a minimal project offline:\n{r.stderr[-2000:]}")
        why.append("uv's offline cache lacks the declared backend")
    else:
        why.append("no uv")
    if bs["build-backend"] != "setuptools.build_meta" or len(bs["requires"]) != 1:
        raise ToolMissing("; ".join(why + ["the pip fallback only knows a setuptools-only build-system"]))
    check = ("import importlib.metadata as m, pip, setuptools.build_meta; "
             "print(m.version('setuptools'))")
    for py in dict.fromkeys(p for p in (os.environ.get("FOREMIND_BUILD_PYTHON"), shutil.which("python3"),
                                        shutil.which("python3.11")) if p):
        r = _run([py, "-c", check])
        if r.returncode == 0 and _satisfies(r.stdout.strip(), bs["requires"][0]):
            return "pip", py
    raise ToolMissing("; ".join(why + [f"no interpreter with pip and {bs['requires'][0]}"]))


def source_copy(dst, src=ROOT) -> Path:
    """The working tree as it is (uncommitted edits included), without VCS data or earlier build output."""
    return Path(shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "build", "dist", "*.egg-info", ".DS_Store")))


def build(out, src=ROOT) -> tuple[Path, Path]:
    """(sdist, wheel) built offline from a temporary copy of `src` into `out`."""
    out = Path(out)
    with tempfile.TemporaryDirectory(prefix="foremind-build-") as tmp:
        tool, py = preflight(tmp, src)
        copy = source_copy(Path(tmp, "src"), src)
        if tool == "uv":  # the sdist first, the wheel from the sdist: what is not in the sdist is not in the wheel
            steps = [["uv", "build", "--offline", "--out-dir", str(out), str(copy)]]
        else:
            steps = [[py, "-c", f"import setuptools.build_meta as b; b.build_sdist({str(out)!r})"],
                     [py, "-m", "pip", "wheel", "--no-build-isolation", "--no-deps", "--no-index", "-w", str(out),
                      str(copy)]]
        for argv in steps:
            r = _run(argv, cwd=copy)
            if r.returncode:
                raise BuildFailed(f"{' '.join(argv)} exited {r.returncode}:\n{r.stdout[-2000:]}\n{r.stderr[-4000:]}")
    sdists, wheels = sorted(out.glob("*.tar.gz")), sorted(out.glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        raise BuildFailed(f"expected one sdist and one wheel in {out}: {sorted(p.name for p in out.iterdir())}")
    return sdists[0], wheels[0]


def _metadata_version(text) -> str:
    return re.search(r"^Version: (.+)$", text, re.M)[1].strip()


class PackagingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="foremind-dist-")
        src = Path(cls.tmp.name, "src")
        source_copy(src)
        # private records present in the copy (a public checkout has none), so leaving them out is shown
        for rel in ("SPEC.md", "PROGRESS.md", "handoff/x.md", "review/x.md", ".foremind/install/state.json"):
            p = src / rel
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("private\n", encoding="utf-8")
        try:
            cls.sdist, cls.wheel = build(Path(cls.tmp.name, "out"), src)
        except ToolMissing as e:
            cls.tmp.cleanup()
            if REQUIRE:
                raise AssertionError(f"FOREMIND_REQUIRE_BUILD=1 but no offline build tool: {e}") from None
            raise unittest.SkipTest(f"no offline build tool: {e}") from None
        except BaseException:
            cls.tmp.cleanup()
            raise
        with zipfile.ZipFile(cls.wheel) as z:
            cls.wheel_files = {n: z.read(n) for n in z.namelist()}
        with tarfile.open(cls.sdist, "r:gz") as t:  # 3.11: no extraction filters needed, nothing is extracted
            top = f"{cls.sdist.name[:-len('.tar.gz')]}/"
            cls.sdist_files = {m.name[len(top):]: t.extractfile(m).read() for m in t.getmembers()
                               if m.isfile() and m.name.startswith(top)}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_ten_templates_in_both(self):
        for files, what in ((self.wheel_files, "wheel"), (self.sdist_files, "sdist")):
            for name in foremind.TEMPLATES:
                path = f"foremind/templates/{name}"
                with self.subTest(what=what, name=name):
                    self.assertIn(path, sorted(files))
                    self.assertEqual(files[path], (ROOT / path).read_bytes())
                    self.assertTrue(files[path].decode("utf-8").strip())
            self.assertFalse([n for n in files if n.startswith("templates/")], f"{what}: the old location")

    def test_license_in_both(self):
        lic = (ROOT / "LICENSE").read_bytes()
        self.assertEqual(self.sdist_files.get("LICENSE"), lic)
        self.assertTrue([n for n, b in self.wheel_files.items()
                         if re.fullmatch(r"foremind-[^/]+\.dist-info/(licenses/)?LICENSE", n) and b == lic],
                        "no LICENSE in the wheel's dist-info")

    def test_one_version(self):
        """CLI version, foremind.__version__ and both artifacts' metadata agree; pyproject holds no version text."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["version"])
        self.assertEqual(out.getvalue().strip(), foremind.__version__)
        info = next(b for n, b in self.wheel_files.items() if re.fullmatch(r"foremind-[^/]+\.dist-info/METADATA", n))
        self.assertEqual(_metadata_version(info.decode()), foremind.__version__)
        self.assertEqual(_metadata_version(self.sdist_files["PKG-INFO"].decode()), foremind.__version__)
        self.assertTrue(self.wheel.name.startswith(f"foremind-{foremind.__version__}-"))
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertNotIn("version", project["project"])
        self.assertIn("version", project["project"]["dynamic"])
        self.assertEqual(project["tool"]["setuptools"]["dynamic"]["version"], {"attr": "foremind.__version__"})

    def test_nothing_private(self):
        for files, what in ((self.wheel_files, "wheel"), (self.sdist_files, "sdist")):
            for name in files:
                with self.subTest(what=what, name=name):
                    self.assertFalse(any(name == p or name.startswith(p) if p.endswith("/") else name == p
                                         for p in PRIVATE))
                    self.assertNotIn("/.foremind/", f"/{name}")

    def test_dependencies_unchanged(self):
        """The #4 credential (Q-34) covers metadata only: no runtime dependency, the same build requirement."""
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["dependencies"], [])
        self.assertEqual(build_system(), {"requires": ["setuptools>=61"], "build-backend": "setuptools.build_meta"})


class HelperTest(unittest.TestCase):  # no build: runs everywhere
    def test_build_env_keeps_caches_and_drops_session_marks(self):
        env = {"HOME": "/home/u", "UV_CACHE_DIR": "/c/uv", "FOREMIND_SESSION": "s", "FOREMIND_ROLE": "seat",
               "PYTHONPATH": "/src", "PATH": "/bin"}
        with mock.patch.dict(os.environ, env, clear=True):
            got = build_env()
        self.assertEqual((got["HOME"], got["UV_CACHE_DIR"], got["PATH"], got["UV_OFFLINE"]),
                         ("/home/u", "/c/uv", "/bin", "1"))
        self.assertFalse({"FOREMIND_SESSION", "FOREMIND_ROLE", "PYTHONPATH"} & set(got))

    def test_satisfies(self):
        self.assertTrue(_satisfies("80.9.0", "setuptools>=61"))
        self.assertTrue(_satisfies("61.0", "setuptools >= 61"))
        self.assertFalse(_satisfies("60.10.1", "setuptools>=61"))
        self.assertFalse(_satisfies("99", "setuptools<70"))  # not understood: never taken as satisfied

    def test_a_candidate_that_does_not_build_fails(self):
        """A candidate with the declared backend that does not build is BuildFailed, not ToolMissing (REQ-2)."""
        with tempfile.TemporaryDirectory() as d:
            src = Path(d, "bad")
            src.mkdir()
            reqs = ", ".join(f'"{r}"' for r in build_system()["requires"])
            (src / "pyproject.toml").write_text(
                f'[build-system]\nrequires = [{reqs}]\nbuild-backend = "{build_system()["build-backend"]}"\n\n'
                '[project]\nname = "bad"\nversion = "0"\n\n[tool.setuptools]\npackages = ["missing_pkg"]\n')
            try:
                preflight(Path(d, "pre"), src)
            except ToolMissing as e:
                if REQUIRE:
                    self.fail(f"FOREMIND_REQUIRE_BUILD=1 but no offline build tool: {e}")
                self.skipTest(f"no offline build tool: {e}")
            self.assertRaises(BuildFailed, build, Path(d, "out"), src)


if __name__ == "__main__":
    unittest.main()
