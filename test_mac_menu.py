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
        active = mac_menu.menu_model("gemini", snap, {"active_sessions": 1, "display_tps": 72.5}, "空闲", now=1000)
        self.assertEqual(active["speed"], "")
        self.assertFalse(active["live"])
        self.assertIsNone(active["tps"])

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

    def test_macos_mimo_reader_reads_the_console(self):
        # The Mac keeps the MiMo session in the keychain, so it reads the
        # console like Windows; without a session refresh() uses the manual plan.
        tracker = Mock()
        tracker.view.return_value = Mock(period_start=None, period_end=None, used_percent=None,
                                         anchor_at=None, error="暂无数据", today_used=None, day=None)
        with patch.object(quota_sources.sys, "platform", "darwin"), \
                patch.object(quota_sources, "mimo_tracker", return_value=tracker):
            quota_sources.get_mimo_quota()
        self.assertIs(tracker.refresh.call_args.args[0], mac_menu.mimo_usage.fetch_with_renewal)

    def test_status_item_matches_the_windows_tray_icon(self):
        import tray_icons
        snap = self.snapshot(secondary_window_minutes=10080, secondary_remaining_percent=12.0, secondary_resets_at=3000)
        for performance in ({}, {"active_sessions": 1, "display_tps": 40.0}):
            self.assertEqual(mac_menu.status_image(snap, "C", performance).tobytes(),
                             tray_icons.make_display_icon(snap, "C", performance).tobytes())

    def test_mono_mode_draws_black_and_white_template_shapes(self):
        import icon_art
        self.addCleanup(icon_art.set_mono, False)
        colour = icon_art.ring_icon((64.0, 12.0), "C")
        icon_art.set_mono(True)
        mono = icon_art.ring_icon((64.0, 12.0), "C")
        self.assertEqual(colour.getpixel((12, 32))[3], 255)                 # the ink disc...
        self.assertLessEqual(mono.getpixel((12, 32))[3], 2)                 # ...is gone: the bar is the ground
        self.assertTrue(all(min(p[:3]) >= 250 for p in mono.getdata() if p[3] > 24))   # white shapes only
        self.assertEqual(mono.getpixel((1, 32))[3], 255)                    # a quota arc is solid
        self.assertEqual(colour.getpixel((47, 58))[3], 255)                 # the empty track: solid grey in colour...
        self.assertTrue(40 < mono.getpixel((47, 58))[3] < 140)              # ...a dim third in mono
        live = icon_art.live_icon(98.0, "A")
        self.assertEqual(live.getpixel((32, 4)), (255, 255, 255, 255))      # generating: a solid disc...
        self.assertLess(min(live.getpixel((x, 32))[3] for x in range(14, 50)), 40)   # ...with the number knocked out
        waiting = icon_art.waiting_icon(3, "C", unit="d")
        self.assertTrue(all(min(p[:3]) >= 250 for p in waiting.getdata() if p[3] > 24))
        self.assertLessEqual(waiting.getpixel((14, 20))[3], 2)              # no disc behind the countdown either
        icon_art.set_mono(False)
        self.assertEqual(icon_art.live_icon(98.0, "A").getpixel((32, 4)), icon_art.accent("A"))   # cache cleared with the mode

    def test_the_settings_page_chooses_which_sources_get_a_menu_bar_icon(self):
        keys = ["codex", "grok", "gemini", "claude", "mimo"]
        self.assertEqual(mac_menu.menu_bar_keys(keys, {}), keys)                                   # nothing saved: all
        self.assertEqual(mac_menu.menu_bar_keys(keys, None), keys)
        self.assertEqual(mac_menu.menu_bar_keys(keys, {"menu_bar": ["claude"]}), ["claude"])
        self.assertEqual(mac_menu.menu_bar_keys(keys, {"menu_bar": ["mimo", "codex"]}), ["codex", "mimo"])
        # Never an empty bar: a choice that names no running source shows them all.
        self.assertEqual(mac_menu.menu_bar_keys(["codex", "grok"], {"menu_bar": ["claude"]}), ["codex", "grok"])
        self.assertEqual(mac_menu.menu_bar_keys(keys, {"menu_bar": []}), keys)
        self.assertEqual(mac_menu.menu_bar_keys(keys, {"menu_bar": "claude"}), keys)               # not a list: ignored

    def test_a_used_up_source_leaves_the_bar_until_its_reset(self):
        from tray_icons import used_up_until
        now = 1_800_000_000.0

        def quota(provider="claude", left=40.0, reset=now + 3600, week=60.0, week_reset=now + 5 * 86400, **extra):
            return QuotaSnapshot(provider=provider, label=provider, window_minutes=300, used_percent=None,
                                 remaining_percent=left, resets_at=reset, updated_at=now - 60, stale=False, error=None,
                                 secondary_window_minutes=10080, secondary_remaining_percent=week,
                                 secondary_resets_at=week_reset, **extra)
        self.assertIsNone(used_up_until(quota(left=3.0), now))                            # nearly empty is still usable
        self.assertEqual(used_up_until(quota(left=0.0), now), now + 3600)                 # 5 h window empty: until it resets
        self.assertEqual(used_up_until(quota(week=0.0), now), now + 5 * 86400)            # weekly empty: until the weekly reset
        self.assertEqual(used_up_until(quota(left=0.0, week=0.0), now), now + 5 * 86400)  # both: the later one
        self.assertIsNone(used_up_until(quota(left=0.0, reset=now - 5), now))             # reset already passed
        self.assertIsNone(used_up_until(quota(left=0.0, reset=None), now))                # reset unknown
        # Purchased credits do not count: the plan quota at 0 % is used up either way.
        self.assertEqual(used_up_until(quota("codex", week=0.0, credits_balance=59618.6), now), now + 5 * 86400)
        self.assertEqual(used_up_until(quota("codex", left=0.0, credits_balance=float("inf")), now), now + 3600)
        self.assertEqual(used_up_until(quota("codex", left=0.0, credits_balance=0.0), now), now + 3600)
        self.assertIsNone(used_up_until(None, now))
        pools = [{"key": "argon", "remaining_percent": 0.0, "resets_at": now + 600},
                 {"key": "gemini", "remaining_percent": 0.0, "resets_at": now + 300}]
        self.assertEqual(used_up_until(quota("gemini", pools=pools), now), now + 300)    # every pool empty: the first back
        pools[0]["remaining_percent"] = 20.0
        self.assertIsNone(used_up_until(quota("gemini", pools=pools), now))              # one pool still has quota

        snapshots = {"claude": quota(left=0.0), "codex": quota("codex")}
        shown, waiting = mac_menu.bar_shown(["claude", "codex"], snapshots, now)
        self.assertEqual((shown, waiting), (["codex"], {"claude": now + 3600}))
        shown, waiting = mac_menu.bar_shown(["claude", "codex"], snapshots, now + 3601)   # past the reset: back
        self.assertEqual((shown, waiting), (["claude", "codex"], {}))
        snapshots["codex"] = quota("codex", week=0.0)
        shown, waiting = mac_menu.bar_shown(["claude", "codex"], snapshots, now)          # all used up: never an empty bar
        self.assertEqual((shown, waiting), (["claude"], {"codex": now + 5 * 86400}))      # the one back first stays
        self.assertEqual(mac_menu.bar_shown(["claude"], {}, now), (["claude"], {}))       # not read yet: shown

    def test_status_items_have_stable_autosave_names(self):
        names = {mac_menu.autosave_name(key) for key in ("claude", "codex", "gemini", "grok", "mimo")}
        self.assertEqual(names, {"Circlow.claude", "Circlow.codex", "Circlow.gemini", "Circlow.grok", "Circlow.mimo"})

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
        # Without Ghostty (test_mac_panel covers the Ghostty window), Terminal hosts Herdr.
        with patch.object(herdr_source.sys, "platform", "darwin"), patch.dict("os.environ", {"CIRCLOW_HERDR_SESSION": ""}), \
                patch.object(herdr_source, "ghostty_app", return_value=None):
            herdr_source.open_window("/opt/homebrew/bin/herdr", lambda args, **_: opened.append(args))
        self.assertEqual(opened[0][:2], ["osascript", "-e"])
        self.assertIn('do script "/opt/homebrew/bin/herdr"', opened[0][2])
        terminal = SimpleNamespace(pid=2, exe=lambda: "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal")
        client = SimpleNamespace(pid=3, info={"name": "herdr", "cmdline": ["/opt/homebrew/bin/herdr"]},
                                 exe=lambda: "/opt/homebrew/bin/herdr", parents=lambda: [terminal])
        with patch.dict("os.environ", {"CIRCLOW_HERDR_SESSION": ""}):
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


@unittest.skipUnless(sys.platform == "darwin", "the macOS fallbacks")
class BubbleOpenMacTests(unittest.TestCase):
    """A click on a task bubble with no direct link to its session (no herdr
    pane, no codex:// or claude:// link): as on Windows, never a new agent."""

    def tray(self, provider):
        import tray_app
        from types import SimpleNamespace
        tray = tray_app.QuotaTray.__new__(tray_app.QuotaTray)
        tray.clusters = {"k": SimpleNamespace(members=[{"key": "s1", "provider": provider, "waiting": "asked", "at": 1}])}
        return tray, tray_app

    def test_claude_and_grok_only_bring_herdr_forward(self):
        tray, tray_app = self.tray("claude")
        with patch.object(tray_app.session_links, "open_session", return_value=None), \
                patch.object(tray_app.herdr_source, "raise_client") as raised, \
                patch.object(tray_app.launchers, "open_mac") as opened, \
                patch.dict(tray_app.PROVIDERS, {"claude": "A"}):
            tray.open_session("k")
        raised.assert_called_once()
        opened.assert_not_called()

    def test_codex_opens_its_desktop_app(self):
        tray, tray_app = self.tray("codex")
        with patch.object(tray_app.session_links, "open_session", return_value=None), \
                patch.object(tray_app.herdr_source, "raise_client") as raised, \
                patch.object(tray_app.launchers, "open_mac") as opened, \
                patch.dict(tray_app.PROVIDERS, {"codex": "C"}):
            tray.open_session("k")
        raised.assert_not_called()
        self.assertEqual(opened.call_args[0][0].target, "com.openai.codex")

    def test_a_direct_link_needs_no_fallback(self):
        tray, tray_app = self.tray("codex")
        with patch.object(tray_app.session_links, "open_session", return_value="url"), \
                patch.object(tray_app.launchers, "open_mac") as opened:
            tray.open_session("k")
        opened.assert_not_called()


class OpenMacTests(unittest.TestCase):
    def test_ways_are_carried_out_or_left_to_the_caller(self):
        import launchers
        run = Mock(return_value=Mock(returncode=0))
        browse = Mock()
        self.assertTrue(launchers.open_mac(launchers.Way("mac-app", "com.openai.codex"), run, browse))
        self.assertEqual(run.call_args[0][0], ["open", "-b", "com.openai.codex"])
        self.assertTrue(launchers.open_mac(launchers.Way("url", "https://example.com"), run, browse))
        browse.assert_called_once_with("https://example.com")
        run.return_value = Mock(returncode=1)
        self.assertFalse(launchers.open_mac(launchers.Way("mac-app", "com.missing.app"), run, browse))
        self.assertFalse(launchers.open_mac(launchers.Way("herdr", "claude"), run, browse))


@unittest.skipUnless(sys.platform != "win32", "POSIX file modes")
class SummaryFileModeTests(unittest.TestCase):
    def test_the_model_config_is_readable_by_this_user_only(self):
        import os
        import agent_tasks
        import settings_window
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(agent_tasks, "SUMMARY_CONFIG", Path(folder) / "agent_summary.json"):
            settings_window.save_summary({"url": "https://token-plan-cn.xiaomimimo.com/v1", "key": "test-only-key",
                                          "model": "mimo-v2.6-flash"}, True)
            self.assertEqual(os.stat(agent_tasks.SUMMARY_CONFIG).st_mode & 0o777, 0o600)
