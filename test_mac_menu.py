"""Contracts for the native macOS status-item view model and POSIX probe."""
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import mac_menu
import quota_sources
from cli_usage import run_temporary_usage
from quota_sources import QuotaSnapshot


class MacMenuTests(unittest.TestCase):
    def snapshot(self, provider="codex", **changes):
        values = dict(provider=provider, label=provider.title(), window_minutes=300,
                      used_percent=25.0, remaining_percent=75.0, resets_at=2000,
                      updated_at=1_800_000_000.0, stale=False, error=None)
        values.update(changes)
        return QuotaSnapshot(**values)

    def test_status_item_shows_both_codex_windows_and_active_tps(self):
        snap = self.snapshot(secondary_window_minutes=10080, secondary_remaining_percent=12.0,
                             secondary_resets_at=3000)
        model = mac_menu.menu_model("codex", snap, {"active_sessions": 1, "display_tps": 82.25},
                                    "下次刷新约 5 分钟后", now=1000)
        self.assertEqual(len(model["quota"]), 2)
        self.assertIn("5 小时 · 剩余 75%", model["quota"][0])
        self.assertIn("每周 · 剩余 12%", model["quota"][1])
        self.assertIn("剩余 12%", model["summary"])
        self.assertEqual(model["speed"], "生成 TPS 82.2")
        self.assertEqual(mac_menu.status_image(snap, "C", {}).size, (64, 64))

    def test_gemini_account_pool_change_uses_only_current_pools(self):
        snap = self.snapshot(provider="gemini", pools=[
            {"key": "gemini", "label": "Gemini", "remaining_percent": 83.0, "resets_at": 2000},
            {"key": "claude_gpt", "label": "Claude/GPT", "remaining_percent": 40.0, "resets_at": 3000},
        ])
        model = mac_menu.menu_model("gemini", snap, {}, "空闲", now=1000)
        self.assertEqual(len(model["quota"]), 2)
        self.assertIn("Gemini", model["quota"][0])
        self.assertIn("Claude/GPT", model["quota"][1])
        self.assertNotIn("Argon", " ".join(model["quota"]))

    def test_mimo_today_credit_line_and_old_data(self):
        snap = self.snapshot(provider="mimo", label="Xiaomi MiMo", window_minutes=525600,
                             today_used=125000.0,
                             today_date=datetime.now().astimezone().date().isoformat(), stale=True)
        model = mac_menu.menu_model("mimo", snap, {}, "空闲", now=time.time())
        self.assertIn("年度套餐额度", model["quota"][0])
        self.assertEqual(model["quota"][1], "今日用量 12.5 万")
        self.assertNotIn("Credits", " ".join(model["quota"]))
        self.assertIn("旧数据", model["summary"])
        snap.today_date = "2020-01-01"
        self.assertEqual(len(mac_menu.menu_model("mimo", snap, {}, "空闲", now=time.time())["quota"]), 1)

    def test_no_claude_plan_does_not_claim_quota(self):
        snap = self.snapshot(provider="claude", remaining_percent=None, used_percent=None,
                             error="未找到订阅额度")
        model = mac_menu.menu_model("claude", snap, {}, "无可用额度", now=1000)
        self.assertIn("额度未知", model["summary"])
        self.assertIn("未找到订阅额度", model["error"])

    def test_macos_mimo_reader_uses_manual_plan_not_console(self):
        tracker = Mock()
        tracker.view.return_value = Mock(period_start=None, period_end=None, used_percent=None,
                                         anchor_at=None, error="暂无数据", today_used=None, day=None)
        with patch.object(quota_sources.sys, "platform", "darwin"), \
                patch.object(quota_sources, "mimo_tracker", return_value=tracker), \
                patch.object(mac_menu.mimo_usage, "fetch_with_renewal",
                             side_effect=AssertionError("console credentials accessed")):
            quota_sources.get_mimo_quota()
        self.assertIs(tracker.refresh.call_args.args[0], mac_menu.mimo_usage.manual_plan)

    def test_status_item_matches_the_windows_tray_icon(self):
        import tray_icons
        snap = self.snapshot(secondary_window_minutes=10080, secondary_remaining_percent=12.0, secondary_resets_at=3000)
        for performance in ({}, {"active_sessions": 1, "display_tps": 40.0}):
            self.assertEqual(mac_menu.status_image(snap, "C", performance).tobytes(),
                             tray_icons.make_display_icon(snap, "C", performance).tobytes())

    def test_status_items_read_in_tray_order_left_to_right(self):
        # macOS puts each new item to the left, so creation runs right to left.
        created = mac_menu.bar_order(["mimo", "codex", "grok", "claude", "gemini"])
        self.assertEqual(list(reversed(created)), ["claude", "codex", "gemini", "grok", "mimo"])

    def test_rows_use_reset_distance_and_codex_credits(self):
        now = 1_800_000_000
        snap = self.snapshot(remaining_percent=0.0, used_percent=100.0, resets_at=now + 4 * 3600 + 32 * 60,
                             credits_balance=59_800.0)
        rows = mac_menu.quota_rows(snap, now)
        self.assertEqual(rows[0], "5 小时 · 已用完 · 购买额度 59.8K · 4小时32分钟后重置")
        self.assertIn("等待重置生效", mac_menu.quota_rows(self.snapshot(resets_at=now - 5), now)[0])

    def test_stale_menu_line_says_when_and_why(self):
        now = time.time()
        snap = self.snapshot(provider="grok", updated_at=now - 600, stale=True,
                             error="读取 /usage 失败：RuntimeError: Grok 官方额度服务出错，暂时读不到额度")
        model = mac_menu.menu_model("grok", snap, {}, "空闲", now=now)
        self.assertTrue(model["error"].startswith("显示 "))
        self.assertTrue(model["error"].endswith("Grok 官方额度服务出错，暂时读不到额度"))

    def test_open_menu_reads_at_the_hover_rate(self):
        state = mac_menu.MenuState.__new__(mac_menu.MenuState)
        state.keys, state.scheduler, state.wake = ["claude"], Mock(), Mock()
        state.scheduler.hover.return_value = True
        state.hover("claude", True)
        state.scheduler.hover.assert_called_once()
        state.wake.set.assert_called_once()
        state.hover("not-a-source", True)                  # a menu without a source title
        self.assertEqual(state.scheduler.hover.call_count, 1)

    def test_claude_and_grok_open_in_herdr_once_per_click(self):
        import threading
        state = mac_menu.MenuState.__new__(mac_menu.MenuState)
        state.lock, state.herdr_opening = threading.RLock(), set()
        release, started = threading.Event(), []

        def start(kind):
            started.append(kind)
            release.wait(5)
            return kind
        with patch.object(mac_menu.herdr_source, "raise_client") as raise_client:
            self.assertTrue(state.open_in_herdr("grok", start))
            self.assertFalse(state.open_in_herdr("grok", start))          # still starting
            release.set()
            for _ in range(100):
                if not state.herdr_opening:
                    break
                time.sleep(0.01)
        self.assertEqual(started, ["grok"])
        raise_client.assert_called_once()

    def test_herdr_on_macos_runs_in_terminal_and_raises_its_app(self):
        import herdr_source
        from types import SimpleNamespace
        opened = []
        with patch.object(herdr_source.sys, "platform", "darwin"), patch.dict("os.environ", {"CODEX_QUOTA_HERDR_SESSION": ""}):
            herdr_source.open_window("/opt/homebrew/bin/herdr", lambda args, **_: opened.append(args))
        self.assertEqual(opened[0][:2], ["osascript", "-e"])
        self.assertIn('do script "/opt/homebrew/bin/herdr"', opened[0][2])
        terminal = SimpleNamespace(pid=2, exe=lambda: "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal")
        client = SimpleNamespace(pid=3, info={"name": "herdr", "cmdline": ["/opt/homebrew/bin/herdr"]},
                                 exe=lambda: "/opt/homebrew/bin/herdr", parents=lambda: [terminal])
        with patch.dict("os.environ", {"CODEX_QUOTA_HERDR_SESSION": ""}):
            self.assertEqual(herdr_source.client_app([client]), "/System/Applications/Utilities/Terminal.app")
        self.assertEqual(herdr_source.terminal_script(['/a b/say "hi"'])[2],
                         'tell application "Terminal" to do script "\'/a b/say \\"hi\\"\'"')

    @unittest.skipUnless(sys.platform == "darwin", "requires the macOS PTY")
    def test_macos_temporary_cli_usage_reaches_prompt_and_parses_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "quota_fixture.py"
            script.write_text("""import sys
print('minimal · /help', flush=True)
if sys.stdin.readline().strip() == '/usage':
    print('Weekly limit: 17%\\nNext reset: October 27, 11:02', flush=True)
""", encoding="utf-8")
            output = run_temporary_usage(sys.executable, ["-u", str(script)], timeout=8)
        self.assertIn("Weekly limit: 17%", output)
        self.assertNotIn("private", output)


if __name__ == "__main__":
    unittest.main()
