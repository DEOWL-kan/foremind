import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from foremind.events import EventLog
from foremind.supervisor import quota
from foremind.supervisor.quota import AVAILABLE, EXHAUSTED, LOW, UNKNOWN, probe_done, probe_due, step

H, M = 3600, 60
T0 = 1_800_000_000.0


def tel(ts, five=None, five_reset=T0 + 3 * H, seven=None, seven_reset=T0 + 5 * 86400):
    """A snapshot; seven=None: it has no seven_day (NULL7: there, but null)."""
    t = {"ts": ts, "five_hour": None if five is None else {"pct": five, "resets_at": five_reset}}
    if seven is not None:
        t["seven_day"] = None if seven is NULL7 else {"pct": seven, "resets_at": seven_reset}
    return t


NULL7 = object()


class StepTest(unittest.TestCase):
    def test_full_cycle_available_low_exhausted_unknown_probe_available(self):
        s = step(None, tel(T0, 50), T0, {})
        self.assertEqual(s["state"], AVAILABLE)
        s = step(s, tel(T0 + 60, 86), T0 + 60, {})
        self.assertEqual(s["state"], LOW)
        s = step(s, tel(T0 + 120, 82), T0 + 120, {})
        self.assertEqual(s["state"], LOW, "hysteresis: low holds until under 80%")
        s = step(s, tel(T0 + 180, 79), T0 + 180, {})
        self.assertEqual(s["state"], AVAILABLE)
        s = step(s, tel(T0 + 240, 91), T0 + 240, {})
        self.assertEqual((s["state"], s["window"], s["resets_at"]), (EXHAUSTED, "5h", T0 + 3 * H))
        s = step(s, tel(T0 + 300, 5), T0 + 300, {})
        self.assertEqual(s["state"], EXHAUSTED, "only the reset (then a probe) ends exhaustion")
        s = step(s, tel(T0 + 300, 5), T0 + 3 * H, {})
        self.assertEqual((s["state"], s["reason"]), (UNKNOWN, "reset"), "a reset turns unknown, never available")
        self.assertTrue(probe_due(s, T0 + 3 * H))
        s = probe_done(s, True, T0 + 3 * H + 10, {})
        self.assertEqual(s["state"], AVAILABLE)
        s = step(s, tel(T0 + 300, 5), T0 + 3 * H + 5 * M, {})
        self.assertEqual(s["state"], AVAILABLE, "a successful probe stands while no newer reading exists")
        s = step(s, tel(T0 + 300, 5), T0 + 3 * H + 16 * M, {})
        self.assertEqual((s["state"], s["reason"]), (UNKNOWN, "stale"))

    def test_pause_line_depends_on_role_group(self):
        t = tel(T0, 92)
        self.assertEqual(step(None, t, T0, {}, "long")["state"], EXHAUSTED)  # 100 - 10% reserve
        self.assertEqual(step(None, t, T0, {}, "oneshot")["state"], LOW)  # 95%
        self.assertEqual(step(None, tel(T0, 95), T0, {}, "oneshot")["state"], EXHAUSTED)
        self.assertEqual(step(None, tel(T0, 81), T0, {"quota.reserve_pct": 20}, "long")["state"], EXHAUSTED)
        self.assertEqual(step(None, tel(T0, 90), T0, {"quota.oneshot_pause_pct": 99}, "oneshot")["state"], LOW)

    def test_null_missing_and_stale_are_unknown(self):
        self.assertEqual(step(None, None, T0, {})["reason"], "no_data")
        self.assertEqual(step(None, tel(T0), T0, {})["reason"], "null")  # five_hour null while exhausted
        self.assertEqual(step(None, tel(T0 - 16 * M, 10), T0, {})["reason"], "stale")
        self.assertEqual(step(None, tel(T0, 10, five_reset=T0 - 1), T0, {})["reason"], "reset")
        for s in (step(None, None, T0, {}), step(None, tel(T0), T0, {})):
            self.assertNotEqual(s["state"], AVAILABLE)

    def test_null_seven_day_is_unknown_and_a_missing_one_drops_the_7d_checks(self):  # SF-5
        self.assertEqual(step(None, tel(T0, 10), T0, {})["state"], AVAILABLE, "no seven_day: no 7d window")
        s = step(None, tel(T0, 10, seven=NULL7), T0, {})
        self.assertEqual((s["state"], s["reason"]), (UNKNOWN, "null_7d"))
        self.assertEqual(step(None, tel(T0, 10, seven=50, seven_reset=T0 - 1), T0, {})["reason"], "reset",
                         "a 7d reading of an older window says nothing either")
        self.assertEqual(step(None, tel(T0, 95, seven=NULL7), T0, {})["state"], EXHAUSTED, "5h exhaustion still wins")
        s = probe_done(s, True, T0 + 10, {})
        self.assertEqual(step(s, tel(T0 + 60, 10, seven=NULL7), T0 + 60, {})["state"], AVAILABLE, "the probe vouches")
        self.assertEqual(step(s, tel(T0 + 60, 87, seven=NULL7), T0 + 60, {})["state"], LOW, "5h counts meanwhile")
        self.assertEqual(step(s, tel(T0 + 16 * M, 10, seven=NULL7), T0 + 16 * M, {})["reason"], "null_7d",
                         "for quota.stale_min only")

    def test_unknown_ends_only_with_a_reading_taken_after_it_began(self):
        reset = T0 + 3 * H
        s = step(step(None, tel(T0, 91), T0, {}), None, reset, {})
        self.assertEqual(s["state"], UNKNOWN)
        new_window = T0 + 8 * H
        self.assertEqual(step(s, tel(reset - 1, 10, five_reset=new_window), reset + 60, {})["state"], UNKNOWN)
        self.assertEqual(step(s, tel(reset + 30, 10, five_reset=new_window), reset + 60, {})["state"], AVAILABLE)
        self.assertEqual(step(None, tel(T0 - 60, 10), T0, {})["state"], AVAILABLE, "no state yet: a fresh reading counts")

    def test_a_probe_success_ahead_of_the_7d_share_is_low(self):  # finding 27
        start = T0 - 86400 / 2  # day 1 of the 7d window: its share is 100/7 %
        ahead = tel(T0 - 10 * M, None, seven=20, seven_reset=start + 7 * 86400)  # 5h null: why it went unknown
        s = step(None, ahead, T0, {})
        self.assertEqual((s["state"], s["reason"]), (UNKNOWN, "null"))
        low = probe_done(s, True, T0 + 10, {}, tel=ahead)
        self.assertEqual((low["state"], low["ok_until"]), (LOW, T0 + 10 + 15 * M))
        self.assertEqual(step(low, ahead, T0 + 60, {})["state"], LOW, "held like a successful probe's available")
        self.assertEqual(probe_done(s, True, T0 + 10, {"quota.pace": False}, tel=ahead)["state"], AVAILABLE)
        for other in (None, tel(T0, None), tel(T0, None, seven=10, seven_reset=start + 7 * 86400),
                      tel(T0, None, seven=90, seven_reset=T0 - 1)):  # none, no 7d, within its share, an older window
            self.assertEqual(probe_done(s, True, T0 + 10, {}, tel=other)["state"], AVAILABLE, other)

    def test_probe_backoff_doubles_and_is_capped_by_the_next_reset(self):
        s = step(step(None, tel(T0, 91), T0, {}), None, T0 + 3 * H, {})  # exhausted (5h), then its reset
        self.assertEqual((s["state"], s["next_reset"]), (UNKNOWN, T0 + 8 * H))
        now = T0 + 3 * H
        s = probe_done(s, False, now, {})
        self.assertEqual(s["next_try"], now + 15 * M)
        self.assertFalse(probe_due(s, now + 14 * M))
        s = probe_done(s, False, now + 15 * M, {})
        self.assertEqual(s["next_try"], now + 45 * M)  # 15 + 30
        for _ in range(5):
            s = probe_done(s, False, now + 60 * M, {})
        self.assertEqual(s["next_try"], T0 + 8 * H, "never later than the next reset")
        self.assertEqual(step(s, None, now + 2 * H, {})["fails"], 7, "failures survive while unknown")

    def test_backoff_without_a_known_reset_is_capped_at_five_hours(self):
        s = step(None, None, T0, {})
        for _ in range(8):
            s = probe_done(s, False, T0, {})
        self.assertEqual(s["next_try"], T0 + 5 * H)

    def test_seven_day_exhaustion_waits_for_the_seven_day_reset(self):
        seven_reset = T0 + 2 * 86400
        s = step(None, tel(T0, 30, seven=91, seven_reset=seven_reset), T0, {})
        self.assertEqual((s["state"], s["window"], s["resets_at"]), (EXHAUSTED, "7d", seven_reset))
        s = step(s, tel(T0 + 3 * H, 0, five_reset=T0 + 8 * H), T0 + 3 * H + 1, {})
        self.assertEqual(s["state"], EXHAUSTED, "no probing at the 5h reset")
        self.assertFalse(probe_due(s, T0 + 3 * H + 1))
        s = step(s, None, seven_reset, {})
        self.assertEqual((s["state"], s["next_reset"]), (UNKNOWN, seven_reset + 7 * 86400))

    def test_raising_the_pause_line_reopens_exhaustion(self):  # finding 1
        seven_reset = T0 + 5 * 86400
        s = step(None, tel(T0, 30, seven=91, seven_reset=seven_reset), T0, {})
        self.assertEqual((s["state"], s["pause"]), (EXHAUSTED, 90))
        self.assertIs(step(s, None, T0 + 60, {"quota.reserve_pct": 20}), s, "a lower line changes nothing")
        self.assertIs(step(s, None, T0 + 60, {}), s)
        s = step(s, None, T0 + 60, {"quota.reserve_pct": 5})
        self.assertEqual((s["state"], s["reason"], s["since"]), (UNKNOWN, "config_changed", T0 + 60))
        self.assertTrue(probe_due(s, T0 + 60))
        cfg = {"quota.reserve_pct": 5}  # a fresh reading judges against the new line
        self.assertEqual(step(s, tel(T0 + 120, 30, seven=91, seven_reset=seven_reset), T0 + 120, cfg)["state"], LOW)
        s = step(s, tel(T0 + 120, 30, seven=96, seven_reset=seven_reset), T0 + 120, cfg)
        self.assertEqual((s["state"], s["pause"]), (EXHAUSTED, 95))
        old = {"state": EXHAUSTED, "since": T0, "window": "7d", "resets_at": seven_reset}  # before the pause field
        self.assertEqual(step(old, None, T0 + 60, {})["reason"], "config_changed")
        s = step(None, tel(T0, 96), T0, {}, "oneshot")
        self.assertEqual(step(s, None, T0 + 60, {"quota.oneshot_pause_pct": 98}, "oneshot")["reason"],
                         "config_changed")

    def test_seven_day_ahead_of_its_daily_share_is_low(self):
        start = T0 - 86400 - 60  # second day of the 7d window: 2/7 ≈ 28.6% allowed
        s = step(None, tel(T0, 10, seven=35, seven_reset=start + 7 * 86400), T0, {})
        self.assertEqual(s["state"], LOW)
        s = step(s, tel(T0 + 1, 10, seven=25, seven_reset=start + 7 * 86400), T0 + 1, {})
        self.assertEqual(s["state"], AVAILABLE)
        self.assertAlmostEqual(quota.pace(8 * 86400, 86400 + 10), 100 / 7)
        s = step(None, tel(T0, 10, seven=35, seven_reset=start + 7 * 86400), T0, {"quota.pace": False})
        self.assertEqual(s["state"], AVAILABLE)  # user turned pacing off: only the 5h low line and the pause lines
        s = step(None, tel(T0, 10, seven=91, seven_reset=start + 7 * 86400), T0, {"quota.pace": False})
        self.assertEqual(s["state"], EXHAUSTED)


class ReadTelemetryTest(unittest.TestCase):
    def test_newest_snapshot_with_iso_and_epoch_times_and_nulls(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        d = root / ".foremind" / "telemetry"
        d.mkdir(parents=True)
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()  # noqa: E731
        (d / "a.statusline.json").write_text(json.dumps({"ts": iso(T0), "rate_limits": {
            "five_hour": {"used_percentage": 99, "resets_at": T0 + H}}}))
        (d / "b.statusline.json").write_text(json.dumps({"ts": iso(T0 + 60), "rate_limits": {
            "five_hour": {"used_percentage": 12, "resets_at": iso(T0 + 2 * H)}, "seven_day": None}}))
        (d / "c.statusline.json").write_text("{not json")
        t = quota.read_telemetry(root)
        self.assertEqual(t, {"ts": T0 + 60, "five_hour": {"pct": 12.0, "resets_at": T0 + 2 * H}, "seven_day": None})
        (d / "d.statusline.json").write_text(json.dumps({"ts": iso(T0 + 120), "rate_limits": None}))
        self.assertEqual(quota.read_telemetry(root)["five_hour"], None)
        self.assertNotIn("seven_day", quota.read_telemetry(root), "none in the snapshot: left out, not null")
        self.assertIsNone(quota.read_telemetry(root / "nowhere"))


class StateFileTest(unittest.TestCase):
    """m2b.10: the state is the account's, at the user level; a project's old .foremind/quota.json is folded in."""

    def setUp(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {"FOREMIND_CONFIG_HOME": str(tmp / "cfg")}))
        self.root = tmp / "shop"
        (self.root / ".foremind").mkdir(parents=True)
        self.old = self.root / ".foremind" / "quota.json"

    def test_path_load_save(self):
        self.assertEqual(quota.state_path(), Path(os.environ["FOREMIND_CONFIG_HOME"]) / "quota.json")
        self.assertEqual(quota.load(), {})
        quota.save({"groups": {"claude/long": {"state": LOW}}})
        self.assertEqual(quota.load(), {"groups": {"claude/long": {"state": LOW}}})
        quota.state_path().write_text("[1]")
        self.assertEqual(quota.load(), {}, "not an object: no state")

    def test_migrate_takes_the_stricter_state_per_group(self):
        self.assertFalse(quota.migrate(self.root, {}), "no old file: nothing to do")
        q = {"groups": {"claude/long": {"state": AVAILABLE}, "claude/oneshot": {"state": EXHAUSTED, "window": "7d"}},
             "paused": ["fm-other-a"], "exhausted_ended": 5}
        self.old.write_text(json.dumps({
            "groups": {"claude/long": {"state": UNKNOWN, "reason": "reset"}, "claude/oneshot": {"state": LOW},
                       "codex/long": {"state": "bogus"}},
            "paused": ["fm-shop-b", "fm-other-a"], "exhausted_ended": 9, "merged_checked": {"p.1": 1}}))
        self.assertTrue(quota.migrate(self.root, q))
        self.assertEqual(quota.load(), q)
        self.assertEqual(q["groups"], {"claude/long": {"state": UNKNOWN, "reason": "reset"},
                                       "claude/oneshot": {"state": EXHAUSTED, "window": "7d"}})
        self.assertEqual((q["paused"], q["exhausted_ended"]), (["fm-other-a", "fm-shop-b"], 9))
        self.assertFalse(self.old.exists(), "the old file is gone (merged_checked with it)")
        ev = [e for e in EventLog(self.root / ".foremind" / "events.jsonl").iter() if e["type"] == "quota_migrated"]
        self.assertEqual([e["taken"] for e in ev], [["claude/long"]])
        self.assertFalse(quota.migrate(self.root, q), "once")

    def test_migrate_into_nothing_takes_the_old_state_and_survives_a_bad_old_file(self):
        self.old.write_text(json.dumps({"groups": {"claude/long": {"state": AVAILABLE}}}))
        q = {}
        quota.migrate(self.root, q)
        self.assertEqual((q["groups"], q["paused"]), ({"claude/long": {"state": AVAILABLE}}, []))
        self.old.write_text("{not json")
        self.assertTrue(quota.migrate(self.root, q))
        self.assertEqual(q["groups"], {"claude/long": {"state": AVAILABLE}})
        self.assertFalse(self.old.exists())


if __name__ == "__main__":
    unittest.main()
