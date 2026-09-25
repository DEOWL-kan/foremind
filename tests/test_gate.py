"""Gate building blocks on real temp git repos: merged criterion, patch-id, dependency manifests, release-check."""
import contextlib
import io
import json
import unittest

from foremind import gate, review
from foremind.cli import main
from foremind.paths import state_dir
from foremind.schemas import EXAMPLES
from test_gate_fixture import Project, sh

CODE = "def f():\n    return 1\n"


class GitCriteriaTest(unittest.TestCase):
    def setUp(self):
        self.p = Project(self)
        self.d = self.p.root / "api"

    def git(self, *args):
        return sh(self.d, "git", *args)

    def write(self, path, text, msg):
        (self.d / path).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def feature(self):
        """Branch `feature` with two commits off main; main then moves on with an unrelated commit."""
        self.base = self.git("rev-parse", "main")
        self.git("checkout", "-q", "-b", "feature")
        self.write("a.py", CODE, "a")
        head = self.write("b.py", "B = 2\n", "b")
        self.git("checkout", "-q", "main")
        self.write("other.txt", "unrelated\n", "main moves")
        return head

    def test_merge_commit(self):
        head = self.feature()
        self.assertFalse(gate.is_merged(self.d, "main", head, self.base))
        self.git("merge", "-q", "--no-ff", "-m", "merge", "feature")
        self.assertTrue(gate.is_merged(self.d, "main", head, self.base))

    def test_squash(self):
        head = self.feature()
        self.git("merge", "-q", "--squash", "feature")
        self.git("commit", "-q", "-m", "squash")
        self.assertTrue(gate.is_merged(self.d, "main", head, self.base))

    def test_rebase(self):
        head = self.feature()
        self.git("cherry-pick", f"{self.base}..feature")  # what a rebase merge does: same changes, new SHAs
        self.assertNotEqual(self.git("rev-parse", "HEAD"), head)
        self.assertTrue(gate.is_merged(self.d, "main", head, self.base))

    def test_indentation_only_difference_is_not_merged(self):
        head = self.feature()
        self.git("merge", "-q", "--squash", "feature")
        (self.d / "a.py").write_text(CODE.replace("    return", "  return"))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "squash, re-indented")
        self.assertFalse(gate.is_merged(self.d, "main", head, self.base))

    def test_head_without_changes_of_its_own_is_not_merged(self):
        """§20 I48: merging an ancestor of the target changes nothing, which alone must not read as merged."""
        p, old = self.p, self.git("rev-parse", "main")
        p.batch()  # fresh worktree at main, review never requested
        self.assertFalse(gate.batch_merged(p.root, "shop.1", p.cfg))
        self.write("other.txt", "target moves\n", "main moves")  # the target moves past the untouched batch
        self.assertFalse(gate.batch_merged(p.root, "shop.1", p.cfg))
        self.assertFalse(gate.is_merged(self.d, "main", old, old))
        review.request(p.root, "shop.1", p.cfg)  # requested with nothing committed: base == head
        self.assertEqual(self.bases(), {"api": old})
        self.assertFalse(gate.batch_merged(p.root, "shop.1", p.cfg))
        head = p.commit()
        self.assertFalse(gate.batch_merged(p.root, "shop.1", p.cfg))
        self.git("merge", "-q", "--no-ff", "-m", "merge", head)
        self.assertTrue(gate.batch_merged(p.root, "shop.1", p.cfg))

    def bases(self):
        pairs = review.batch_repos(self.p.root, review.load_batch(self.p.root, "shop.1"), self.p.cfg)
        return gate.bases(self.p.root, "shop.1", pairs, review.heads(pairs))

    def test_base_of_a_head_the_worktree_left_is_not_used(self):
        """Review requested at H1 (base B1), the target moves to M1, the worktree is reset onto M1: B1 belongs to
        H1, not to M1; with it, merging M1 into the target (a no-op) would read as merged."""
        p = self.p
        p.batch()
        p.commit()
        review.request(p.root, "shop.1", p.cfg)
        self.write("other.txt", "target moves\n", "main moves")
        sh(p.wt(), "git", "reset", "-q", "--hard", "main")
        self.assertEqual(self.bases(), {})
        self.assertFalse(gate.batch_merged(p.root, "shop.1", p.cfg))
        p.commit(text="x = 2\n")  # the seat goes on from there: still no base until review is requested again
        self.assertEqual(self.bases(), {})

    def test_patch_id_verbatim(self):
        self.git("checkout", "-q", "-b", "four")
        self.write("a.py", CODE, "four spaces")
        self.git("checkout", "-q", "-b", "two", "main")
        self.write("a.py", CODE.replace("    return", "  return"), "two spaces")
        self.git("checkout", "-q", "main")
        four, two = gate.patch_id(self.d, "main...four"), gate.patch_id(self.d, "main...two")
        self.assertEqual(len(four), 40)
        self.assertNotEqual(four, two)  # indentation is meaningful
        stable = [sh(self.d, "sh", "-c", f"git diff main...{b} | git patch-id --stable").split()[0] for b in ("four", "two")]
        self.assertEqual(stable[0], stable[1])  # ...which plain patch-id would miss
        self.write("other.txt", "moved on\n", "main moves")
        self.git("checkout", "-q", "-b", "four-rebased")
        self.git("cherry-pick", "four")
        self.assertEqual(gate.patch_id(self.d, "main...four-rebased"), four)  # same change on a new base
        self.assertEqual(gate.patch_id(self.d, "main...main"), "")


class ManifestTest(unittest.TestCase):
    def test_package_json(self):
        old = json.dumps({"name": "x", "version": "1", "dependencies": {"a": "1"}, "devDependencies": {"t": "1"}})
        cases = {
            json.dumps({"name": "x", "version": "2", "dependencies": {"a": "1"}, "devDependencies": {"t": "1"}}): 0,
            json.dumps({"name": "x", "version": "1", "dependencies": {"a": "1"}, "devDependencies": {"t": "2"}}): 3,
            json.dumps({"name": "x", "version": "1", "dependencies": {"a": "2"}, "devDependencies": {"t": "1"}}): 4,
            json.dumps({"name": "x", "version": "1", "dependencies": {"a": "1"}, "devDependencies": {"t": "1"},
                        "overrides": {"a": "3"}}): 4,
            "{not json": 4,
        }
        for new, need in cases.items():
            with self.subTest(new=new):
                self.assertEqual(gate.manifest_need("web/package.json", old, new), need)

    def test_pyproject(self):
        old = '[project]\nname = "x"\nversion = "1"\ndependencies = []\n'
        self.assertEqual(gate.manifest_need("pyproject.toml", old, old.replace('"1"', '"2"')), 0)
        self.assertEqual(gate.manifest_need("pyproject.toml", old, old.replace("[]", '["requests"]')), 4)
        self.assertEqual(gate.manifest_need("pyproject.toml", old, old + '[project.optional-dependencies]\ncli = ["rich"]\n'), 4)
        self.assertEqual(gate.manifest_need("pyproject.toml", old, old + '[dependency-groups]\ntest = ["pytest"]\n'), 3)
        self.assertEqual(gate.manifest_need("pyproject.toml", old, old + '[tool.poetry.dependencies]\nrequests = "*"\n'), 4)
        self.assertEqual(gate.manifest_need("pyproject.toml", old, old + '[tool.ruff]\nline-length = 120\n'), 0)
        self.assertEqual(gate.manifest_need("pyproject.toml", None, old.replace("[]", '["requests"]')), 4)  # new file
        self.assertEqual(gate.manifest_need("pyproject.toml", old, "[project\n"), 4)  # cannot parse -> #4
        self.assertEqual(gate.manifest_need("requirements.txt", "a==1\n", "a==2\n"), 4)
        self.assertEqual(gate.manifest_need("requirements.txt", "a==1\n", "a==1\n"), 0)

    def test_manifest_paths(self):
        for path in ("requirements/base.txt", "svc/requirements/dev.in", "environment.yml", "src/App/App.csproj",
                     "deno.json", "web/package.json", "requirements-dev.txt"):
            self.assertTrue(gate.is_manifest(path), path)
        for path in ("docs/requirements.md", "src/app.py", "requirements/README.md"):
            self.assertFalse(gate.is_manifest(path), path)


def pending(qid, state, **kw):
    return {**EXAMPLES["pending"], "id": qid, "state": state, "answer": 1, **kw}


class ReleaseCheckTest(unittest.TestCase):
    def setUp(self):
        self.p = p = Project(self)
        self.d = p.root / "api"
        self.base = sh(self.d, "git", "rev-parse", "HEAD")
        for msg in ("feat: a", "feat: b\n\nprovisional: PV-1"):
            sh(self.d, "git", "commit", "-q", "--allow-empty", "-m", msg)
        p.write_header("shop.1", state="running")
        self.ddir = state_dir(p.root) / "decisions"
        self.ddir.mkdir()
        self.put("Q-1", pending("Q-1", "provisional", blocks=["shop.1"]))
        self.put("PV-1", {**EXAMPLES["provisional"], "id": "PV-1", "question": "Q-1", "batch": "shop.1"})

    def put(self, name, obj):
        (self.ddir / f"{name}.json").write_text(json.dumps(obj))

    def check(self, rng=None):
        return gate.release_check(self.p.root, self.d, rng or f"{self.base}..main")

    def test_provisional_commit_in_range_blocks(self):
        probs = self.check()
        self.assertEqual(len(probs), 1)
        self.assertIn("Q-1 is provisional", probs[0])
        self.assertEqual(self.check(f"{self.base}..main~1"), [])  # marker outside the range, batch still running
        self.put("Q-1", pending("Q-1", "confirmed", blocks=["shop.1"]))
        self.assertEqual(self.check(), [])

    def test_shipped_batch_or_untied_decision_blocks(self):
        self.put("Q-1", pending("Q-1", "confirmed", blocks=["shop.1"]))
        self.p.write_header("shop.2", state="merged")
        self.put("Q-2", pending("Q-2", "overdue", blocks=["shop.2"]))
        self.assertIn("batches ['shop.2']", " ".join(self.check()))
        self.put("Q-2", pending("Q-2", "overdue", blocks=[]))
        self.assertIn("not tied to a batch", " ".join(self.check()))
        self.put("Q-2", pending("Q-2", "overdue", blocks=["shop.1"]))  # shop.1 still running
        self.assertEqual(self.check(), [])

    def test_marker_without_record_blocks(self):
        (self.ddir / "PV-1.json").unlink()
        self.assertIn("PV-1: a commit", " ".join(self.check()))

    def test_range_is_never_an_option(self):
        with self.assertRaises(review.FlowError):
            self.check("--all")  # without --end-of-options git log would take it as an option and read every ref

    def test_cli(self):
        (self.p.root / "foremind.toml").write_text('[[repos]]\nid = "api"\npath = "api"\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["release-check", f"{self.base}..main"]), 1)
            self.put("Q-1", pending("Q-1", "confirmed", blocks=["shop.1"]))
            self.assertEqual(main(["release-check", f"{self.base}..main", "--repo", "api"]), 0)
        self.assertIn("release-check: clean", out.getvalue())


if __name__ == "__main__":
    unittest.main()
