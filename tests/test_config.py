import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foremind.config import AUTHZ, PLAIN, ConfigError, Layer, key_class, load, merge


def L(name, data, approved=False):
    return Layer(name, data, approved)


class MergeTest(unittest.TestCase):
    def test_four_classes(self):
        cfg = merge([
            L("user", {"review": {"max_rounds": 3}, "exclude": {"models": ["claude:*sonnet*"]},
                       "delivery": {"level": {"ceiling": "merge_dev", "value": "done"}}}),
            L("project", {"review": {"max_rounds": 5}, "exclude": {"models": ["x", "claude:*sonnet*"]},
                          "delivery": {"level": "merge_dev"}}),
            L("delivery", {"delivery": {"repo": {"backend": {"merge_method": "squash", "target_branch": "develop"}}}}),
            L("task", {"review": {"max_rounds": 2}, "exclude": {"models": ["y"]}, "delivery": {"level": "done"}}),
        ])
        self.assertEqual(cfg["review.max_rounds"], 2)
        self.assertEqual(cfg["exclude.models"], ["claude:*sonnet*", "x", "y"])
        self.assertEqual(cfg["delivery.repo.backend.merge_method"], "squash")
        self.assertEqual(cfg["delivery.repo.backend.target_branch"], "develop")
        self.assertEqual(cfg["delivery.level"], "done")

    def test_layers_are_applied_in_canonical_order(self):
        cfg = merge([L("task", {"review": {"max_rounds": 2}}), L("user", {"review": {"max_rounds": 9}})])
        self.assertEqual(cfg["review.max_rounds"], 2)
        with self.assertRaises(ConfigError):
            merge([L("session", {})])

    def test_union_can_not_drop_upper_entries(self):
        cfg = merge([L("user", {"gate": {"checks": ["a", "b"]}}), L("task", {"gate": {"checks": []}})])
        self.assertEqual(cfg["gate.checks"], ["a", "b"])
        with self.assertRaises(ConfigError):
            merge([L("user", {"gate": {"checks": "a"}})])

    def test_value_above_ceiling_raises(self):
        with self.assertRaises(ConfigError) as cm:
            merge([L("user", {"delivery": {"level": {"ceiling": "done", "value": "merge_dev"}}})])
        self.assertIn("delivery.level", str(cm.exception))
        self.assertIn("user", str(cm.exception))
        # a later layer tightening the ceiling below the inherited value is an error, not a silent clamp
        with self.assertRaises(ConfigError) as cm:
            merge([L("user", {"delivery": {"level": "merge_dev"}}),
                   L("project", {"delivery": {"level": {"ceiling": "done"}}})])
        self.assertIn("project", str(cm.exception))

    def test_lower_layer_ceiling_cannot_be_wider(self):
        with self.assertRaises(ConfigError) as cm:  # project cannot raise the user's ceiling back up
            merge([L("user", {"delivery": {"level": {"ceiling": "done"}}}),
                   L("project", {"delivery": {"level": {"ceiling": "merge_dev"}}})])
        self.assertIn("ceiling", str(cm.exception))
        self.assertIn("project", str(cm.exception))
        cfg = merge([L("user", {"delivery": {"level": {"ceiling": "merge_dev"}}}),  # equal or tighter is fine
                     L("project", {"delivery": {"level": {"ceiling": "merge_dev"}}}),
                     L("delivery", {"delivery": {"level": {"ceiling": "done"}}})])
        self.assertEqual(cfg["delivery.level"], "done")

    def test_numeric_order(self):
        def reserve(*layers):
            return merge([L(n, {"quota": {"reserve_pct": v}}, a) for n, v, a in layers])["quota.reserve_pct"]
        # bigger reserve = stricter: the ceiling is the smallest reserve allowed
        self.assertEqual(reserve(("user", {"ceiling": 10, "value": 20}, False), ("task", 30, False)), 30)
        self.assertEqual(reserve(("user", {"ceiling": 10, "value": 20}, False), ("task", 15, True)), 15)
        self.assertEqual(reserve(("user", {"ceiling": 10}, False)), 10)
        self.assertEqual(reserve(("user", 20, False), ("task", 5, True)), 5)  # approved task may widen without a ceiling
        self.assertEqual(reserve(("user", 10**400, False)), 10**400)  # huge int: no OverflowError from isfinite
        for layers in (
            [("user", {"ceiling": 10, "value": 5}, False)],                          # below the ceiling
            [("user", {"ceiling": 10}, False), ("project", {"ceiling": 5}, False)],  # ceiling widened
            [("user", 20, False), ("task", 15, False)],                              # task widens unapproved
            [("user", "high", False)],                                               # not a number
            [("user", float("nan"), False)],                                         # NaN / inf are no numbers here
            [("user", 10, False), ("task", float("inf"), True)],
        ):
            with self.assertRaises(ConfigError):
                reserve(*layers)

    def test_unset_value_is_strictest(self):
        cfg = merge([L("user", {"delivery": {"level": {"ceiling": "merge_dev"}}})])
        self.assertEqual(cfg["delivery.level"], "done")

    def test_unknown_level_raises(self):
        with self.assertRaises(ConfigError):
            merge([L("user", {"delivery": {"level": "deploy"}})])

    def test_task_widening_needs_approval(self):
        layers = [L("user", {"delivery": {"level": {"ceiling": "merge_dev", "value": "done"}}})]
        with self.assertRaises(ConfigError) as cm:
            merge(layers + [L("task", {"delivery": {"level": "merge_dev"}})])
        self.assertIn("delivery.level", str(cm.exception))
        self.assertIn("task", str(cm.exception))
        cfg = merge(layers + [L("task", {"delivery": {"level": "merge_dev"}}, approved=True)])
        self.assertEqual(cfg["delivery.level"], "merge_dev")
        # approval never lifts the ceiling
        with self.assertRaises(ConfigError):
            merge([L("user", {"delivery": {"level": {"ceiling": "done"}}}),
                   L("task", {"delivery": {"level": "merge_dev"}}, approved=True)])

    def test_task_tightening_is_free(self):
        cfg = merge([L("project", {"gate": {"rereview_after_update": "delta"}}),
                     L("task", {"gate": {"rereview_after_update": "full"}})])
        self.assertEqual(cfg["gate.rereview_after_update"], "full")

    def test_per_repo_authz_wildcard(self):
        with self.assertRaises(ConfigError):
            merge([L("delivery", {"delivery": {"repo": {"api": {"level": "done"}}}}),
                   L("task", {"delivery": {"repo": {"api": {"level": "merge_dev"}}}})])

    def test_repo_convention_rejected_in_task(self):
        merge([L("project", {"delivery": {"repo": {"api": {"update_method": "rebase"}}}})])
        with self.assertRaises(ConfigError) as cm:
            merge([L("task", {"delivery": {"repo": {"api": {"update_method": "merge"}}}}, approved=True)])
        self.assertIn("delivery.repo.api.update_method", str(cm.exception))
        self.assertEqual(key_class("delivery.level"), AUTHZ)  # a repo id can not shadow fixed delivery keys
        with self.assertRaises(ConfigError):
            merge([L("task", {"repos": []})])

    def test_unregistered_key_is_authz(self):
        self.assertEqual(key_class("some.new_key"), AUTHZ)
        self.assertEqual(key_class("flow.S.steps"), PLAIN)
        user = L("user", {"some": {"new_key": "a"}})
        for name in ("project", "delivery"):  # without an order only the user layer may change it
            self.assertEqual(merge([user, L(name, {"some": {"new_key": "a"}})])["some.new_key"], "a")
            with self.assertRaises(ConfigError) as cm:
                merge([user, L(name, {"some": {"new_key": "b"}})])
            self.assertIn(name, str(cm.exception))
        with self.assertRaises(ConfigError):  # nor may another layer introduce it
            merge([L("project", {"some": {"new_key": "a"}})])
        self.assertEqual(merge([user, L("task", {"some": {"new_key": "a"}})])["some.new_key"], "a")
        with self.assertRaises(ConfigError) as cm:
            merge([user, L("task", {"some": {"new_key": "b"}})])
        self.assertIn("some.new_key", str(cm.exception))
        with self.assertRaises(ConfigError):  # a task cannot introduce an unknown key either
            merge([L("task", {"other": 1})])
        self.assertEqual(merge([user, L("task", {"some": {"new_key": "b"}}, approved=True)])["some.new_key"], "b")

    def test_unordered_ceilings_must_agree(self):
        with self.assertRaises(ConfigError):
            merge([L("user", {"k": {"ceiling": "a"}}), L("project", {"k": {"ceiling": "b"}})])
        with self.assertRaises(ConfigError):
            merge([L("user", {"k": {"ceiling": "a", "value": "b"}})])
        with self.assertRaises(ConfigError):  # approval lets a task change the value, never the ceiling
            merge([L("user", {"k": {"ceiling": "a"}}), L("task", {"k": {"ceiling": "b"}}, approved=True)])

    def test_unordered_same_value_as_ceiling_only_is_fine(self):
        cfg = merge([L("user", {"k": {"ceiling": "a"}}), L("project", {"k": "a"}), L("task", {"k": "a"})])
        self.assertEqual(cfg["k"], "a")
        with self.assertRaises(ConfigError):
            merge([L("user", {"k": {"ceiling": "a"}}), L("project", {"k": "b"})])

    def test_value_only_table_is_authz_only_where_the_key_is(self):
        self.assertEqual(merge([L("user", {"stuck": {"value": 5}})]), {"stuck.value": 5})  # stuck.* is PLAIN
        self.assertEqual(merge([L("user", {"some": {"key": {"value": "a"}}})]), {"some.key": "a"})
        self.assertEqual(merge([L("user", {"flow": {"S": {"ceiling": 1}}})]), {"flow.S.ceiling": 1})

    def test_task_layer_from_header(self):
        from foremind.config import task_layer
        self.assertEqual(task_layer({}), {})
        header = {"id": "M1.1", "config": {"review": {"max_rounds": 2}, "hard_block": {"categories": ["db", "pay"]}},
                  "hard_block": ["pay", "auth"]}
        t = task_layer(header)
        self.assertEqual(t, {"review.max_rounds": 2, "hard_block.categories": ["db", "pay", "auth"]})
        self.assertEqual(header["config"]["hard_block"]["categories"], ["db", "pay"])  # input untouched
        self.assertEqual(task_layer({"hard_block": ["x"]}), {"hard_block.categories": ["x"]})
        cfg = merge([L("user", {"hard_block": {"categories": ["db"]}}), L("task", t)])
        self.assertEqual(cfg["hard_block.categories"], ["db", "pay", "auth"])
        for bad in ({"config": "x"}, {"hard_block": "x"}, {"config": {"hard_block": 1}, "hard_block": ["a"]},
                    {"config": {"hard_block": {"categories": ["a"]}, "hard_block.categories": ["b"]}}):  # same key twice
            with self.assertRaises(ConfigError):
                task_layer(bad)

    def test_task_layer_dotted_config_keeps_both_sides(self):
        from foremind.config import task_layer
        t = task_layer({"config": {"hard_block.categories": ["db"]}, "hard_block": ["pay"]})
        cfg = merge([L("user", {"hard_block": {"categories": ["x"]}}), L("task", t)])
        self.assertEqual(cfg["hard_block.categories"], ["x", "db", "pay"])


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.tmp / "home"
        self.root = self.tmp / "proj"
        (self.root / ".foremind").mkdir(parents=True)
        self.home.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {"FOREMIND_CONFIG_HOME": str(self.home)}))

    def test_reads_all_layers(self):
        (self.home / "config.toml").write_text('[exclude]\nmodels = ["a"]\n[delivery.level]\nceiling = "merge_dev"\n')
        (self.root / "foremind.toml").write_text(
            '[review]\nmax_rounds = 4\n[exclude]\nmodels = ["b"]\n[[repos]]\nid = "api"\npath = "api"\n')
        (self.root / ".foremind" / "config.toml").write_text('[review]\nmax_rounds = 6\n')
        (self.root / ".foremind" / "delivery.toml").write_text('[delivery]\nlevel = "merge_dev"\n')
        cfg = load(self.root, {"exclude": {"models": ["c"]}})
        self.assertEqual(cfg["review.max_rounds"], 6)
        self.assertEqual(cfg["exclude.models"], ["a", "b", "c"])
        self.assertEqual(cfg["repos"], [{"id": "api", "path": "api"}])
        self.assertEqual(cfg["delivery.level"], "merge_dev")
        self.assertEqual(load(self.root, {"delivery": {"level": "done"}})["delivery.level"], "done")
        with self.assertRaises(ConfigError):
            load(self.root, {"repos": []})

    def test_missing_files_and_bad_toml(self):
        self.assertEqual(load(self.root), {})
        (self.root / "foremind.toml").write_text("not = [toml")
        with self.assertRaises(ConfigError) as cm:
            load(self.root)
        self.assertIn("foremind.toml", str(cm.exception))

    def test_non_utf8_file_is_a_config_error(self):
        (self.home / "config.toml").write_bytes(b"# \xff\n")  # say, a GBK comment
        with self.assertRaises(ConfigError) as cm:
            load(self.root)
        self.assertIn("layer user", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
