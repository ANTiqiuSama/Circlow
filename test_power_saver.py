"""Battery saver: power detection, longer idle reads on battery (back to the
normal cadence near a reset), the cheap new-output check between TPS scans,
the tray's quiet state, and the live sync sleeping until its next round."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import power_source
import sync_hub
import tray_app
from refresh_scheduler import ACTIVE, BATTERY_IDLE, BATTERY_NEAR_RESET, HOVER, IDLE, RefreshScheduler
from tps_sources import TpsMonitor


def reading(resets_at=None, secondary=None, pools=None, error=None):
    return SimpleNamespace(error=error, resets_at=resets_at, secondary_resets_at=secondary, pools=pools)


class PowerSourceTests(unittest.TestCase):
    def test_override_wins_and_unknown_platform_is_mains(self):
        self.assertTrue(power_source.on_battery(override="battery"))
        self.assertFalse(power_source.on_battery(override="AC"))
        self.assertFalse(power_source.on_battery(platform="linux", override=""))

    def test_watch_asks_again_only_after_its_interval(self):
        answers = iter([True, False])
        calls = []

        def read():
            calls.append(1)
            return next(answers)
        watch = power_source.PowerWatch(read, every=20)
        self.assertTrue(watch.battery(100))
        self.assertTrue(watch.battery(119))
        self.assertEqual(len(calls), 1)
        self.assertFalse(watch.battery(120))
        self.assertEqual(len(calls), 2)

    def test_a_failing_read_counts_as_mains_power(self):
        def read():
            raise OSError("no IOKit")
        self.assertFalse(power_source.PowerWatch(read).battery(0))


class BatterySchedulerTests(unittest.TestCase):
    def scheduler(self, *providers, battery=True):
        scheduler = RefreshScheduler(providers or ("codex",), 0)
        scheduler.set_battery(battery, 0)
        scheduler.take_due(0)
        return scheduler

    def test_suggested_battery_intervals(self):
        self.assertEqual(BATTERY_IDLE["codex"], 600)
        self.assertEqual(BATTERY_IDLE["claude"], 600)
        self.assertEqual(BATTERY_IDLE["gemini"], 600)
        self.assertEqual(BATTERY_IDLE["grok"], 1800)
        for provider, seconds in BATTERY_IDLE.items():
            self.assertGreater(seconds, IDLE[provider])

    def test_idle_on_battery_waits_the_battery_interval(self):
        scheduler = self.scheduler("codex", "grok")
        scheduler.completed("codex", reading(), 100)
        scheduler.completed("grok", reading(), 100)
        self.assertEqual(scheduler.due["codex"], 100 + 600)
        self.assertEqual(scheduler.due["grok"], 100 + 1800)
        self.assertIn("省电", scheduler.describe("codex", 100))

    def test_mains_power_keeps_the_normal_interval(self):
        scheduler = self.scheduler(battery=False)
        scheduler.completed("codex", reading(), 100)
        self.assertEqual(scheduler.due["codex"], 100 + IDLE["codex"])

    def test_the_last_minutes_before_a_reset_use_the_normal_interval(self):
        scheduler = self.scheduler()
        reset = 100 + BATTERY_NEAR_RESET - 60
        scheduler.completed("codex", reading(resets_at=reset), 100)
        self.assertEqual(scheduler.due["codex"], 100 + IDLE["codex"])
        self.assertEqual(scheduler.reason["codex"], "临近重置")

    def test_a_battery_wait_ends_where_the_near_reset_span_begins(self):
        scheduler = self.scheduler()
        reset = 100 + BATTERY_NEAR_RESET + 300          # the span starts 300 s from now
        scheduler.completed("codex", reading(secondary=reset), 100)
        self.assertEqual(scheduler.due["codex"], 100 + 300)

    def test_model_pool_resets_count_too(self):
        scheduler = self.scheduler("gemini")
        reset = 100 + 120
        scheduler.completed("gemini", reading(pools=[{"key": "gemini", "resets_at": reset}]), 100)
        self.assertEqual(scheduler.due["gemini"], 100 + IDLE["gemini"])

    def test_hover_and_work_are_not_slowed(self):
        scheduler = self.scheduler("codex", "claude")
        scheduler.completed("codex", reading(), 10)
        scheduler.completed("claude", reading(), 10)
        scheduler.hover("codex", True, 50)
        scheduler.activity("claude", True, False, 50)
        self.assertEqual(scheduler.take_due(50), ["codex"])
        scheduler.completed("codex", reading(), 100)
        self.assertEqual(scheduler.due["codex"], 100 + HOVER["codex"])
        self.assertEqual(scheduler.take_due(scheduler.due["claude"]), ["claude"])
        scheduler.completed("claude", reading(), 100)
        self.assertEqual(scheduler.due["claude"], 100 + ACTIVE["claude"])

    def test_back_on_mains_power_no_read_waits_past_the_normal_interval(self):
        scheduler = self.scheduler()
        scheduler.completed("codex", reading(), 100)
        self.assertTrue(scheduler.set_battery(False, 130))
        self.assertEqual(scheduler.due["codex"], 100 + IDLE["codex"])
        self.assertFalse(scheduler.set_battery(False, 131))


class StirredTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        folder = self.home / ".claude/projects/demo"
        folder.mkdir(parents=True)
        self.path = folder / "session.jsonl"
        self.path.write_text(json.dumps({"type": "user"}) + "\n", encoding="utf-8")
        self.monitor = TpsMonitor(home=self.home)

    def append(self):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "user"}) + "\n")

    def test_new_output_in_a_recently_active_record_is_noticed(self):
        now = time.time()
        self.monitor.poll(now)
        self.assertIn(self.path, self.monitor.readers)
        self.assertFalse(self.monitor.stirred(now))
        self.append()
        self.assertFalse(self.monitor.stirred(now))      # not active this run yet: the next full scan finds it
        self.monitor.poll(now + 1)
        self.assertIn(self.path, self.monitor.hot)
        self.assertFalse(self.monitor.stirred(now + 2))
        self.append()
        self.assertTrue(self.monitor.stirred(now + 2))
        self.monitor.poll(now + 3)
        self.assertFalse(self.monitor.stirred(now + 3))

    def test_a_record_quiet_for_an_hour_is_no_longer_watched(self):
        now = time.time()
        self.monitor.poll(now)
        self.append()
        self.monitor.poll(now + 1)
        self.append()
        self.assertFalse(self.monitor.stirred(now + 1 + 3601))
        self.assertNotIn(self.path, self.monitor.hot)

    def test_a_changed_codex_stream_log_counts(self):
        now = time.time()
        self.monitor.poll(now)
        codex = self.home / ".codex"
        codex.mkdir()
        (codex / "logs_2.sqlite").write_bytes(b"x")
        self.assertTrue(self.monitor.stirred(now))


class QuietTray:
    looked_at = tray_app.QuotaTray.looked_at
    saving = tray_app.QuotaTray.saving
    scan_due = tray_app.QuotaTray.scan_due

    def __init__(self, battery=True):
        self.power = SimpleNamespace(battery=lambda now: battery)
        self.icons = {"claude": SimpleNamespace(details_visible=False, hover_open=False),
                      "codex": SimpleNamespace(details_visible=False, hover_open=False)}
        self.working: set[str] = set()
        self.stirs = False
        self.tps_monitor = SimpleNamespace(last_output_at=0.0, stirred=lambda now: self.stirs)
        self.stamp = (1, 1)
        self.claude_status_seen = (1, 1)
        self.scanned_at = 1000.0

    def source_working(self, provider):
        return provider in self.working

    def claude_status_stamp(self):
        return self.stamp


@patch.object(tray_app, "PROVIDERS", {"claude": "A", "codex": "C"})
class TrayQuietTests(unittest.TestCase):
    def test_saving_needs_battery_and_nothing_going_on(self):
        tray = QuietTray()
        self.assertTrue(tray.saving(1000))
        self.assertFalse(QuietTray(battery=False).saving(1000))
        tray.working = {"codex"}
        self.assertFalse(tray.saving(1000))
        tray.working = set()
        tray.tps_monitor.last_output_at = 1000 - tray_app.QUIET_AFTER + 5
        self.assertFalse(tray.saving(1000))
        tray.tps_monitor.last_output_at = 0
        tray.icons["claude"].details_visible = True       # the panel is open
        self.assertFalse(tray.saving(1000))

    def test_while_saving_the_records_are_read_only_when_they_moved(self):
        tray = QuietTray()
        self.assertFalse(tray.scan_due(1005, True))
        self.assertTrue(tray.scan_due(1005, False))
        self.assertTrue(tray.scan_due(1000 + tray_app.QUIET_SCAN, True))
        tray.stirs = True
        self.assertTrue(tray.scan_due(1005, True))
        tray.stirs = False
        tray.stamp = (2, 1)                               # Claude Code's status line moved
        self.assertTrue(tray.scan_due(1005, True))


class LiveLoopSleepTests(unittest.TestCase):
    def test_run_sleeps_until_the_round_and_wake_cuts_it_short(self):
        rounds = []
        loop = sync_hub.LiveLoop(SimpleNamespace(), "mac", lambda now: {})
        stop = threading.Event()

        def tick(now):
            rounds.append(now)
            loop.next_at = now + 60
            if len(rounds) >= 2:
                stop.set()
        loop.tick = tick
        worker = threading.Thread(target=loop.run, args=(stop,), daemon=True)
        worker.start()
        time.sleep(0.3)
        self.assertEqual(len(rounds), 1)                  # no half-second polling
        loop.wake()
        worker.join(2)
        self.assertEqual(len(rounds), 2)


if __name__ == "__main__":
    unittest.main()
