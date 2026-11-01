"""Platform-neutral tests: forecast, refresh scheduling, detail card, quota parsing."""
import math
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import quota_sources
from forecast import QuotaHistory, Sample, current_window, duration_text, forecast
from refresh_scheduler import DURATION_GAP, HOVER, ACTIVE, IDLE, RETRY, SETTLE, RefreshScheduler

HOUR = 3600


class ForecastTests(unittest.TestCase):
    def test_provider_history_clear_keeps_other_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            history = QuotaHistory(Path(folder) / "history.json")
            for name in ("gemini", "gemini:secondary", "codex"):
                history.add(name, 1000, 20, 9000)
            history.clear("gemini")
            self.assertEqual(history.samples("gemini"), [])
            self.assertEqual(history.samples("gemini:secondary"), [])
            self.assertEqual(len(history.samples("codex")), 1)

    def series(self, now, used_values, step=600, resets_at=None):
        start = now - step * (len(used_values) - 1)
        return [Sample(start + i * step, used, resets_at) for i, used in enumerate(used_values)]

    def test_steady_burn_predicts_exhaustion_before_reset(self):
        now = 1_000_000.0
        reset = int(now + 10 * HOUR)
        samples = self.series(now, [40, 45, 50, 55, 60], resets_at=reset)  # 30 %/h
        result = forecast(samples, now, 40, reset)
        self.assertAlmostEqual(result.rate_per_hour, 30, places=3)
        self.assertTrue(result.before_reset)
        self.assertAlmostEqual(result.exhausts_at - now, 80 * 60, delta=1)
        self.assertEqual(result.tone, "warn")
        self.assertIn("早于重置", result.text)

    def test_slow_burn_is_enough_until_reset(self):
        now = 1_000_000.0
        reset = int(now + 2 * HOUR)
        samples = self.series(now, [10, 11, 12], resets_at=reset)  # 6 %/h
        result = forecast(samples, now, 88, reset)
        self.assertFalse(result.before_reset)
        self.assertEqual(result.tone, "ok")
        self.assertIn("够用", result.text)

    def test_reset_inside_series_is_not_negative_usage(self):
        now = 1_000_000.0
        samples = self.series(now, [90, 95, 2, 6, 10])
        self.assertEqual([s.used for s in current_window(samples, None)], [2, 6, 10])
        self.assertGreater(forecast(samples, now, 90, None).rate_per_hour, 0)

    def test_other_reset_windows_are_ignored(self):
        now = 1_000_000.0
        old = self.series(now - 6 * HOUR, [10, 50, 90], resets_at=int(now - HOUR))
        new = self.series(now, [5, 5, 5], resets_at=int(now + 4 * HOUR))
        result = forecast(old + new, now, 95, int(now + 4 * HOUR))
        self.assertEqual(result.tone, "idle")

    def test_too_little_history_says_so(self):
        now = 1_000_000.0
        self.assertEqual(forecast([Sample(now - 60, 10, None), Sample(now, 12, None)], now, 88, None).tone, "unknown")
        self.assertEqual(forecast([], now, None, None).tone, "unknown")
        self.assertEqual(forecast([], now, 0, int(now + 60)).tone, "danger")

    def test_duration_text_uses_days_for_weekly_windows(self):
        self.assertEqual(duration_text(2 * 86400 + 5 * 3600), "2 天")      # days only past a day
        self.assertEqual(duration_text(86400 + 3600), "1 天")
        self.assertEqual(duration_text(23 * 3600 + 50 * 60), "1 天")
        self.assertEqual(duration_text(3 * 3600 + 120), "3 小时")       # hours only from an hour up
        self.assertEqual(duration_text(3 * 3600 + 40 * 60), "4 小时")   # rounded to the nearest hour
        self.assertEqual(duration_text(35 * 60), "35 分钟")
        self.assertEqual(duration_text(20), "1 分钟")

    def test_history_records_fresh_reads_only_and_round_trips(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            history = QuotaHistory(path)
            fresh = quota_sources.QuotaSnapshot("codex", "Codex", 300, 20, 80, 2_000_000, 1_000_000, False, None,
                                                10080, 30, 70, 2_500_000)
            stale = quota_sources.QuotaSnapshot("codex", "Codex", 300, 25, 75, 2_000_000, 1_000_100, True, "x")
            self.assertTrue(history.record(fresh))
            self.assertFalse(history.record(fresh))  # Same reading.
            self.assertFalse(history.record(stale))
            history.save()
            loaded = QuotaHistory(path)
            self.assertEqual(loaded.samples("codex"), [Sample(1_000_000, 20, 2_000_000)])
            self.assertEqual(loaded.samples("codex:secondary"), [Sample(1_000_000, 30, 2_500_000)])


class SchedulerTests(unittest.TestCase):
    def snapshot(self, error=None, resets_at=None, secondary=None):
        return SimpleNamespace(error=error, resets_at=resets_at, secondary_resets_at=secondary)

    def test_startup_reads_everything_once(self):
        scheduler = RefreshScheduler(("codex", "grok"), 100)
        self.assertEqual(scheduler.take_due(100), ["codex", "grok"])
        self.assertEqual(scheduler.take_due(100), [])  # Already running.

    def test_idle_providers_wait_long_and_grok_longest(self):
        scheduler = RefreshScheduler(("codex", "grok"), 0)
        scheduler.take_due(0)
        scheduler.completed("codex", self.snapshot(), 0)
        scheduler.completed("grok", self.snapshot(), 0)
        self.assertEqual(scheduler.due["codex"], IDLE["codex"])
        self.assertEqual(scheduler.due["grok"], IDLE["grok"])
        self.assertGreater(IDLE["grok"], IDLE["codex"])

    def test_finished_generation_refreshes_after_settle_delay(self):
        scheduler = RefreshScheduler(("codex",), 0)
        scheduler.take_due(0)
        scheduler.completed("codex", self.snapshot(), 0)
        scheduler.activity("codex", True, False, 10)
        self.assertGreaterEqual(scheduler.due["codex"], ACTIVE["codex"])  # No refresh storm on start.
        self.assertTrue(scheduler.activity("codex", False, False, 30))
        self.assertEqual(scheduler.due["codex"], 30 + SETTLE["codex"])

    def test_continuous_use_refreshes_at_active_interval(self):
        scheduler = RefreshScheduler(("codex",), 0)
        scheduler.take_due(0)
        scheduler.completed("codex", self.snapshot(), 990)
        scheduler.activity("codex", True, False, 1000)
        first = 1000 + ACTIVE["codex"]
        self.assertEqual(scheduler.due["codex"], first)  # One active interval into use, not instantly.
        scheduler.take_due(first)
        scheduler.completed("codex", self.snapshot(), first)
        self.assertEqual(scheduler.due["codex"], first + ACTIVE["codex"])

    def test_hover_is_fastest_then_active_then_idle_for_every_source(self):
        for provider in IDLE:
            with self.subTest(provider=provider):
                self.assertLess(HOVER[provider], ACTIVE[provider])
                self.assertLess(ACTIVE[provider], IDLE[provider])
        self.assertEqual(max(IDLE, key=IDLE.get), "grok")        # its reader opens a whole CLI session

    def test_every_hover_reads_now_then_at_the_hover_interval(self):
        from refresh_scheduler import HOVER_REPEAT
        scheduler = RefreshScheduler(("codex",), 0)
        scheduler.take_due(0)
        scheduler.completed("codex", self.snapshot(), 100)
        self.assertEqual(scheduler.due["codex"], 100 + IDLE["codex"])
        self.assertTrue(scheduler.hover("codex", True, 104))        # read 4 s ago, but a new hover: read now
        self.assertEqual((scheduler.due["codex"], scheduler.reason["codex"]), (104, "悬停"))
        scheduler.take_due(104)
        scheduler.completed("codex", self.snapshot(), 105)
        self.assertEqual(scheduler.due["codex"], 105 + HOVER["codex"])       # then the hover cadence
        scheduler.hover("codex", False, 106)
        self.assertTrue(scheduler.hover("codex", True, 106.5))     # out and back at once: the same look,
        self.assertEqual(scheduler.due["codex"], 105 + HOVER_REPEAT)        # read again only after a moment
        scheduler.take_due(108)
        scheduler.hover("codex", False, 109)
        self.assertFalse(scheduler.hover("codex", True, 112))      # comes back while that read runs
        scheduler.completed("codex", self.snapshot(), 113)
        self.assertEqual((scheduler.due["codex"], scheduler.reason["codex"]), (113, "悬停"))   # one more after it
        scheduler.take_due(113)
        scheduler.hover("codex", False, 114)
        scheduler.completed("codex", self.snapshot(), 115)          # pointer gone: back to the idle cadence
        self.assertEqual(scheduler.due["codex"], 115 + IDLE["codex"])
        scheduler.hover("codex", False, 503)
        scheduler.activity("codex", True, False, 503)
        scheduler.take_due(scheduler.due["codex"])
        scheduler.completed("codex", self.snapshot(), 600)
        self.assertEqual((scheduler.due["codex"], scheduler.reason["codex"]), (600 + ACTIVE["codex"], "使用中"))

    def test_a_slow_read_is_not_repeated_back_to_back(self):
        scheduler = RefreshScheduler(("grok",), 0)
        scheduler.hover("grok", True, 0)
        scheduler.take_due(0)
        scheduler.completed("grok", self.snapshot(), 40, duration=35)      # a whole Grok CLI session
        self.assertEqual(scheduler.due["grok"], 40 + max(HOVER["grok"], DURATION_GAP * 35))

    def test_hover_speeds_up_a_used_up_quota_but_not_an_account_without_one(self):
        scheduler = RefreshScheduler(("grok",), 0)
        scheduler.take_due(0)
        used_up = SimpleNamespace(error=None, remaining_percent=0, resets_at=3 * 86400, secondary_remaining_percent=None,
                                  secondary_resets_at=None, window_minutes=10080, secondary_window_minutes=None)
        scheduler.completed("grok", used_up, 0)
        self.assertGreater(scheduler.due["grok"], IDLE["grok"])        # far from the reset: parked
        later = HOVER["grok"] + 10
        scheduler.hover("grok", True, later)
        self.assertEqual(scheduler.due["grok"], later)
        scheduler.take_due(later)
        scheduler.completed("grok", used_up, later + 20)
        self.assertEqual(scheduler.due["grok"], later + 20 + HOVER["grok"])
        dormant = RefreshScheduler(("claude",), 0)
        dormant.take_due(0)
        dormant.completed("claude", self.snapshot(error="未找到订阅额度"), 0)
        self.assertFalse(dormant.hover("claude", True, 100))

    def test_reset_time_pulls_refresh_forward(self):
        scheduler = RefreshScheduler(("codex",), 0)
        scheduler.take_due(0)
        scheduler.completed("codex", self.snapshot(resets_at=120, secondary=90), 0)
        self.assertEqual(scheduler.due["codex"], 95)
        self.assertIn("额度重置", scheduler.describe("codex", 0))

    def test_failures_back_off(self):
        scheduler = RefreshScheduler(("codex",), 0)
        delays = []
        now = 0
        for _ in range(6):
            scheduler.take_due(now)
            scheduler.completed("codex", self.snapshot(error="x"), now)
            delays.append(scheduler.due["codex"] - now)
            now = scheduler.due["codex"]
        # Retries back off, but never wait longer than the idle interval.
        self.assertEqual(delays[:4], [min(delay, IDLE["codex"]) for delay in RETRY])
        self.assertEqual(delays[-1], min(RETRY[-1], IDLE["codex"]))

    def test_exhausted_grok_waits_then_polls_near_reset(self):
        from refresh_scheduler import EXHAUSTED, NEAR_RESET
        scheduler = RefreshScheduler(("grok",), 0)
        lead, poll = NEAR_RESET["grok"]
        reset = 400_000
        empty = self.snapshot(resets_at=reset)
        empty.remaining_percent, empty.window_minutes = 0.0, 10080

        def read(now):
            scheduler.take_due(now)
            scheduler.completed("grok", empty, now)
            return scheduler.due["grok"] - now, scheduler.reason["grok"]

        self.assertEqual(read(0), (EXHAUSTED["grok"], "额度用尽"))
        self.assertEqual(read(reset - lead - 600), (600, "额度用尽"))           # wakes exactly at the lead
        self.assertEqual(read(reset - lead), (poll, "临近重置"))
        self.assertEqual(read(reset - 400), (100, "临近重置"))                  # lands on the final 5 minutes
        self.assertEqual(read(reset - 200), (60, "临近重置"))
        # The reset time passed but the CLI still says used up: keep checking.
        self.assertEqual(read(reset + 30), (60, "等待重置生效"))
        self.assertEqual(read(reset + 1200), (poll, "等待重置生效"))
        self.assertEqual(read(reset + 7200), (IDLE["grok"], "等待重置生效"))

    def test_exhausted_with_unknown_or_implausible_reset_is_not_parked_for_hours(self):
        scheduler = RefreshScheduler(("grok",), 0)
        for reset in (None, 400 * 86400):
            empty = self.snapshot(resets_at=reset)
            empty.remaining_percent, empty.window_minutes = 0.0, 10080
            scheduler.take_due(0)
            scheduler.completed("grok", empty, 0)
            self.assertEqual(scheduler.due["grok"], IDLE["grok"])

    def test_reset_date_that_just_passed_is_not_moved_to_next_year(self):
        from datetime import datetime, timedelta
        from cli_usage import parse_reset
        for offset, expect_shift in ((timedelta(hours=-2), 0), (timedelta(days=2), 0), (timedelta(days=-10), 1)):
            moment = (datetime.now() + offset).replace(second=0, microsecond=0)
            text = f"Weekly limit: 100%\nNext reset: {moment.strftime('%B')} {moment.day}, {moment.strftime('%H:%M')}"
            expected = moment.replace(year=moment.year + expect_shift)
            self.assertEqual(parse_reset(text), int(expected.timestamp()), text)

    def test_codex_used_up_keeps_normal_cadence_then_fast_after_reset(self):
        scheduler = RefreshScheduler(("codex",), 0)
        empty = self.snapshot(resets_at=10_000)
        empty.remaining_percent, empty.window_minutes = 0.0, 300
        scheduler.take_due(0)
        scheduler.completed("codex", empty, 0)
        self.assertEqual(scheduler.due["codex"], IDLE["codex"])
        scheduler.take_due(10_010)
        scheduler.completed("codex", empty, 10_010)
        self.assertEqual(scheduler.due["codex"], 10_070)

    def test_claude_without_subscription_is_checked_rarely_and_ignores_activity(self):
        from refresh_scheduler import GIVE_UP
        scheduler = RefreshScheduler(("claude",), 0)
        scheduler.take_due(0)
        scheduler.completed("claude", self.snapshot(error="读取 /usage 失败：ValueError: Claude /usage 未找到订阅额度（API Key 计费账号没有 5 小时/每周额度）"), 0)
        self.assertEqual(scheduler.due["claude"], GIVE_UP["claude"])
        self.assertFalse(scheduler.activity("claude", True, False, 10))
        self.assertFalse(scheduler.activity("claude", False, True, 20))
        self.assertEqual(scheduler.due["claude"], GIVE_UP["claude"])
        scheduler.request(["claude"], 30)
        self.assertEqual(scheduler.due["claude"], 30)

    def test_claude_repeated_failures_give_up(self):
        from refresh_scheduler import GIVE_UP
        scheduler = RefreshScheduler(("claude",), 0)
        now = 0
        for _ in range(4):
            scheduler.take_due(now)
            scheduler.completed("claude", self.snapshot(error="timeout"), now)
            now = scheduler.due["claude"]
        self.assertEqual(scheduler.reason["claude"], "多次失败")
        self.assertGreaterEqual(scheduler.due["claude"] - now + (now - scheduler.last_done["claude"]), GIVE_UP["claude"])

    def test_manual_and_wake_requests_are_immediate_and_sleep_is_bounded(self):
        scheduler = RefreshScheduler(("codex",), 0)
        scheduler.take_due(0)
        scheduler.completed("codex", self.snapshot(), 0)
        self.assertLessEqual(scheduler.next_wake(0), 30)
        scheduler.request(None, 50)
        self.assertEqual(scheduler.take_due(50), ["codex"])
        self.assertEqual(scheduler.due["codex"], math.inf)


class QuotaParsingTests(unittest.TestCase):
    def test_codex_secondary_window_is_read_and_binding(self):
        snapshot = quota_sources.parse_codex_rate_limits({
            "primary": {"usedPercent": 13, "windowDurationMins": 300, "resetsAt": 1_800_000_000},
            "secondary": {"usedPercent": 71, "windowDurationMins": 10080, "resetsAt": 1_800_400_000},
        })
        self.assertEqual(snapshot.remaining_percent, 87)
        self.assertEqual(snapshot.secondary_remaining_percent, 29)
        self.assertEqual(snapshot.secondary_window_minutes, 10080)
        self.assertEqual(snapshot.display_remaining, 29)

    def test_codex_without_secondary_still_works(self):
        snapshot = quota_sources.parse_codex_rate_limits({"primary": {"usedPercent": 40}})
        self.assertIsNone(snapshot.secondary_remaining_percent)
        self.assertEqual(snapshot.display_remaining, 60)

    def test_collect_runs_providers_in_parallel_and_keeps_last_good(self):
        def slow(name, error=None):
            def read():
                time.sleep(0.3)
                return quota_sources._unknown(name, name, 300, error) if error else quota_sources.QuotaSnapshot(
                    name, name, 300, 10, 90, None, time.time(), False, None)
            return read
        old = quota_sources.QuotaSnapshot("grok", "Grok", 10080, 30, 70, None, 1.0, False, None)
        originals = quota_sources.get_codex_quota, quota_sources.get_grok_quota, quota_sources.get_gemini_quota
        quota_sources.get_codex_quota = slow("codex")
        quota_sources.get_grok_quota = slow("grok", "boom")
        quota_sources.get_gemini_quota = slow("gemini")
        try:
            started = time.monotonic()
            with patch.object(quota_sources.providers, "enabled_keys", return_value=["codex", "grok", "gemini"]):
                result = quota_sources.collect_all({"grok": old})
            elapsed = time.monotonic() - started
        finally:
            quota_sources.get_codex_quota, quota_sources.get_grok_quota, quota_sources.get_gemini_quota = originals
        self.assertLess(elapsed, 0.8)
        self.assertEqual(result["grok"].remaining_percent, 70)
        self.assertTrue(result["grok"].stale)
        self.assertEqual(result["codex"].remaining_percent, 90)


if __name__ == "__main__":
    unittest.main()
