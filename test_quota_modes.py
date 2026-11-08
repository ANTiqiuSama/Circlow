"""Quota warnings are not evidence of paid usage, on either desktop frontend."""
import dataclasses
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import mac_menu
import sync_hub
from forecast import QuotaHistory
from quota_sources import QuotaSnapshot
from tray_app import QuotaTray


class QuotaModeTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.tray = QuotaTray.__new__(QuotaTray)
        self.tray.snapshots = {}
        self.tray.agents = []
        self.tray.performance = {"providers": {}}
        self.tray.agent_groups = SimpleNamespace(compose=lambda agents: [])
        self.tray.tps_monitor = SimpleNamespace(events=[])
        self.tray.history = None

    def reading(self, five=92.0, week=6.0, provider="codex", credits=60_000):
        snapshot = QuotaSnapshot(provider, provider, 300, 100-five, five,
                                 int(self.now+3600), self.now, False, None,
                                 10080, None if week is None else 100-week, week,
                                 int(self.now+4*86400), credits_balance=credits)
        self.tray.snapshots[provider] = snapshot
        self.tray.performance["providers"][provider] = {"active_sessions": 1}
        return snapshot

    def test_positive_windows_never_claim_extra_credit_mode(self):
        for provider in ("codex", "claude", "grok"):
            for five, week in ((92, 6), (6, 92), (1, 1), (0.1, 60), (60, 0.1)):
                with self.subTest(provider=provider, five=five, week=week):
                    self.reading(five, week, provider)
                    model = self.tray.panel(provider)
                    self.assertNotIn("额度模式", model.status)
                    self.assertNotIn("购买额度", model.status)
                    self.assertIn("在工作", model.status)
                    self.assertFalse(any(model.quota_figures))
                    self.assertNotIn("已用完", model.forecast)

    def test_positive_five_hour_window_stays_on_its_own_chart(self):
        with tempfile.TemporaryDirectory() as folder:
            self.tray.history = QuotaHistory(Path(folder) / "history.json")
            for five in (10, 6, 1, 0.1):
                with self.subTest(five=five):
                    self.reading(five, 60)
                    self.assertFalse(self.tray.codex_quota_mode())
                    series = self.tray.burn_chart("codex", self.now).series
                    self.assertEqual([(s.label, s.slot) for s in series], [("Codex · 5 小时", 0)])

    def test_purchased_credits_replace_only_a_truly_empty_window(self):
        self.reading(0, 6)
        self.assertEqual(self.tray.exhausted_state("codex"), ("paid", (0,)))
        model = self.tray.panel("codex")
        self.assertIsNotNone(model.figure_of(0))
        self.assertIsNone(model.figure_of(1))
        self.assertEqual(model.quota[1][1], 6)
        self.assertNotIn("购买额度", model.quota[1][2])
        self.assertNotIn("每周额度已用完", model.forecast)
        self.assertNotIn("4天", model.forecast)  # only the empty 5-hour window must recover

    def test_empty_weekly_window_does_not_replace_positive_five_hour_reading(self):
        self.reading(6, 0)
        self.assertEqual(self.tray.exhausted_state("codex"), ("paid", (1,)))
        model = self.tray.panel("codex")
        self.assertIsNone(model.figure_of(0))
        self.assertIsNotNone(model.figure_of(1))
        self.assertFalse(self.tray.codex_quota_mode())

    def test_only_zero_five_hour_plus_work_enables_weekly_mode(self):
        self.reading(0, 60, credits=0)
        self.assertTrue(self.tray.codex_quota_mode())
        self.assertEqual(self.tray.panel("codex").status, "额度模式工作")
        self.tray.performance["providers"]["codex"]["active_sessions"] = 0
        self.assertFalse(self.tray.codex_quota_mode())
        self.assertEqual(self.tray.panel("codex").status, "等待重置")

    def test_stale_or_failed_reading_cannot_enable_weekly_mode(self):
        snapshot = self.reading(0, 60)
        for changes in ({"stale": True}, {"error": "读取超时"}):
            with self.subTest(changes=changes):
                self.tray.snapshots["codex"] = dataclasses.replace(snapshot, **changes)
                self.assertFalse(self.tray.codex_quota_mode())
                self.assertEqual(self.tray.exhausted_state("codex"), (None, ()))

    def test_idle_low_quota_is_only_a_warning(self):
        self.reading(92, 6)
        self.tray.performance["providers"]["codex"]["active_sessions"] = 0
        self.assertEqual(self.tray.panel("codex").status, "接近用尽")
        self.assertFalse(self.tray.codex_quota_mode())

    def test_mac_menu_keeps_positive_percent_with_purchased_credits_available(self):
        for five, week in ((92, 6), (6, 92), (0.1, 6)):
            with self.subTest(five=five, week=week):
                rows = mac_menu.quota_rows(self.reading(five, week), self.now)
                self.assertTrue(all("剩余" in row for row in rows))
                self.assertFalse(any("已用完" in row or "购买额度" in row for row in rows))

    def test_mac_menu_marks_only_actual_zero_as_purchased(self):
        rows = mac_menu.quota_rows(self.reading(0, 6), self.now)
        self.assertIn("购买额度", rows[0])
        self.assertIn("剩余 6%", rows[1])
        self.assertNotIn("购买额度", rows[1])

    def test_legacy_and_current_synced_readings_use_same_mode_decision(self):
        snapshot = self.reading()
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                remote = dataclasses.replace(snapshot, updated_at=self.now+1).to_dict()
                if legacy:
                    # Ignore an older client's presentation fields; recompute from numbers.
                    remote.update(quota_mode=True, status="额度模式工作")
                payloads = {"mac": {"payload": {"snapshots": {"codex": remote}}}}
                taken = sync_hub.merge_snapshots(payloads, "win", self.tray.snapshots,
                                                self.tray.accept_snapshot)
                self.assertEqual(len(taken), 1)
                self.assertNotIn("额度模式", self.tray.panel("codex").status)
                self.tray.snapshots["codex"] = snapshot

    def test_remote_activity_with_positive_quota_shows_working(self):
        self.reading(92, 60)
        local = {"providers": {"codex": {"active_sessions": 0}}}
        remote = {"mac": {"received_at": self.now, "payload": {"label": "Mac", "providers": {
            "codex": {"active": 2, "tps": 40, "age": 0, "tokens_60": 80}}}}}
        self.tray.performance = sync_hub.combine(local, remote, self.now, "win", self.now)
        self.assertEqual(self.tray.panel("codex").status, "2 个在工作")


if __name__ == "__main__":
    unittest.main()
