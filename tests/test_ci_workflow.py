"""m3a REQ-3: .github/workflows/ci.yml, read with the standard library (JSON is YAML): the four combinations of
Python 3.11 / 3.13 and macOS / Linux each run the full suite and the install smoke with FOREMIND_REQUIRE_BUILD=1, on a
read-only token, with no publishing, pushing or release step and FOREMIND_IT unset. The remote run is not checked here."""
import json
import re
import unittest
from pathlib import Path

CI = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
FULL = "python3 -m unittest discover -s tests"
SMOKE = "python3 -m unittest discover -s tests -p 'test_install_smoke.py'"
# a step that publishes, pushes, tags or releases (REQ-3: build and verify only)
OUTWARD = re.compile(r"git\s+push|git\s+tag|\bgh\s+(release|pr|repo)\b|twine|uv\s+publish|pypi|"
                     r"action-gh-release|create-release|upload-release", re.I)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.wf = json.loads(CI.read_text(encoding="utf-8"))
        (self.name, self.job), = self.wf["jobs"].items()
        self.steps = self.job["steps"]
        self.runs = [s["run"] for s in self.steps if "run" in s]

    def test_four_combinations_each_run_the_suite_and_the_smoke(self):
        m = self.job["strategy"]["matrix"]
        self.assertEqual(set(m), {"os", "python"})
        self.assertEqual(sorted(m["os"]), ["macos-latest", "ubuntu-latest"])
        self.assertEqual(sorted(m["python"]), ["3.11", "3.13"])
        self.assertIs(self.job["strategy"]["fail-fast"], False)  # one red cell does not hide the others
        self.assertEqual(self.job["runs-on"], "${{ matrix.os }}")
        setup = [s for s in self.steps if s.get("uses", "").startswith("actions/setup-python@")]
        self.assertEqual([s["with"]["python-version"] for s in setup], ["${{ matrix.python }}"])
        self.assertIn(FULL, self.runs)
        self.assertTrue([r for r in self.runs if r.startswith(SMOKE)], self.runs)
        self.assertFalse([s for s in self.steps if "if" in s or s.get("continue-on-error")], "no step is optional")

    def test_builds_are_required_and_the_runner_has_no_identity_of_its_own(self):
        self.assertEqual(self.job["env"]["FOREMIND_REQUIRE_BUILD"], "1")
        self.assertFalse(any("FOREMIND_REQUIRE_BUILD" in s.get("env", {}) for s in self.steps), "set once, for all")
        self.assertIn("GIT_CONFIG_GLOBAL=$RUNNER_TEMP/empty-gitconfig", "\n".join(self.runs))
        # wheel <0.46 (the last with its own bdist_wheel), not a newer setuptools: an image's setuptools <70.1 stays
        # and the old bdist_wheel path is exercised
        self.assertTrue(any('pip install "setuptools>=61" "wheel<0.46"' in r for r in self.runs),
                        "the declared backend, and a bdist_wheel for a setuptools below 70.1")

    def test_read_only_and_nothing_outward(self):
        self.assertEqual(self.wf["permissions"], {"contents": "read"})
        self.assertNotIn("permissions", self.job)
        self.assertEqual(set(self.wf["on"]), {"push", "pull_request"})
        text = CI.read_text(encoding="utf-8")
        self.assertNotIn("FOREMIND_IT", text)
        self.assertNotIn("secrets.", text)
        for s in self.steps:
            with self.subTest(step=s.get("name") or s.get("uses")):
                self.assertIsNone(OUTWARD.search(json.dumps(s)))
                if "uses" in s:
                    self.assertRegex(s["uses"], r"^actions/(checkout|setup-python)@v\d+$")
        checkout = next(s for s in self.steps if s.get("uses", "").startswith("actions/checkout@"))
        self.assertIs(checkout["with"]["persist-credentials"], False)


if __name__ == "__main__":
    unittest.main()
