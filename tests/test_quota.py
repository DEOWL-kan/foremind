import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

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

    def test_seven_day_ahead_of_its_daily_share_is_low(self):
        start = T0 - 86400 - 60  # second day of the 7d window: 2/7 ≈ 28.6% allowed
        s = step(None, tel(T0, 10, seven=35, seven_reset=start + 7 * 86400), T0, {})
        self.assertEqual(s["state"], LOW)
        s = step(s, tel(T0 + 1, 10, seven=25, seven_reset=start + 7 * 86400), T0 + 1, {})
        self.assertEqual(s["state"], AVAILABLE)
        self.assertAlmostEqual(quota.pace(8 * 86400, 86400 + 10), 100 / 7)


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


if __name__ == "__main__":
    unittest.main()
