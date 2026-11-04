import threading
from types import SimpleNamespace
import time
import unittest

import agent_panel
from unittest.mock import Mock, call, patch

import pystray
from PIL import Image

import tray_app
from tray_app import PROVIDERS, QuotaTray, reset_countdown, startup_command
from hover_details import HoverDetails, HoverIcon, TrackingTooltip, is_window_covered, tooltip_position, wintypes
from refresh_scheduler import RefreshScheduler
from quota_sources import QuotaSnapshot


class TrayMenuTests(unittest.TestCase):
    def test_packaged_restart_owns_new_python_runtime(self):
        with patch.object(tray_app.sys, 'frozen', True, create=True), \
                patch.dict(tray_app.os.environ, {'EXISTING_SETTING':'keep'}):
            result = tray_app.restart_environment()
        self.assertEqual(result['PYINSTALLER_RESET_ENVIRONMENT'], '1')
        self.assertEqual(result['EXISTING_SETTING'], 'keep')

    def setUp(self):
        # Exercise the real pystray menu validation without quota requests.
        self.tray = QuotaTray.__new__(QuotaTray)
        self.tray.open_provider = Mock()
        self.tray.open_details = Mock()
        self.tray.request_refresh = Mock()

    def test_all_provider_menus_construct_and_default_clicks_stay_bound(self):
        menus = {provider: self.tray.build_menu(provider) for provider in PROVIDERS}
        icon = Mock(spec=pystray.Icon)
        # Invoke after ALL menus are built to catch accidental late binding.
        for provider, menu in menus.items():
            with self.subTest(provider=provider):
                defaults = [item for item in menu.items if item.default]
                self.assertEqual(len(defaults), 1)
                self.assertEqual(defaults[0].text, "打开对应程序")
                menu(icon)
        self.assertEqual(self.tray.open_provider.call_args_list,
                         [call(provider) for provider in PROVIDERS])
        self.tray.request_refresh.assert_not_called()

    def test_context_menu_refresh_and_details_still_dispatch(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                menu = self.tray.build_menu(provider)
                items = {item.text: item for item in menu.items[:4]}
                items["额度详情"](None)
                self.tray.open_details.assert_called_with(provider)
                refresh = items["立即刷新"]
                refresh(None)
        self.assertEqual(self.tray.request_refresh.call_count, len(PROVIDERS))

    def test_claude_menu_choice_persists_and_requests_clean_restart(self):
        self.tray.exit_app = Mock()
        for name in ("menu_quota", "forecast_line", "menu_reset", "menu_countdown", "menu_updated", "menu_reset_outlook"):
            setattr(self.tray, name, Mock(return_value=""))
        for enabled in (False, True):
            active = {"codex": "C", **({"claude": "A"} if enabled else {})}
            with self.subTest(enabled=enabled), patch.object(tray_app, "PROVIDERS", active), \
                    patch.object(tray_app.provider_registry, "set_claude_enabled") as save:
                item = next(item for item in self.tray.build_menu("codex").items
                            if item.text == "Claude Code 额度与 TPS")
                self.assertEqual(item.checked, enabled)
                item(None)
                save.assert_called_once_with(not enabled)
                self.assertTrue(self.tray.restart_requested)
        self.assertEqual(self.tray.exit_app.call_count, 2)


class HoverTests(unittest.TestCase):
    def test_dwell_live_updates_switch_and_leave(self):
        popup = Mock()
        text = Mock(return_value="first sample")
        hover = HoverDetails({}, text, threading.Event())
        rect = (1600, 1030, 1624, 1054)
        hover.update(popup, "codex", rect, 10)
        hover.update(popup, "codex", rect, 10.3)
        popup.show.assert_not_called()
        hover.update(popup, "codex", rect, 10.5)
        popup.show.assert_called_once_with("first sample", rect)
        text.return_value = "second sample"
        popup.hide.reset_mock()
        hover.update(popup, "codex", rect, 11.5)
        popup.show.assert_called_with("second sample", rect)
        popup.hide.assert_not_called()
        hover.update(popup, "grok", rect, 12)
        popup.hide.assert_called_once()
        popup.show.reset_mock()
        hover.update(popup, None, None, 12.5)
        hover.update(popup, "grok", rect, 13)
        popup.show.assert_not_called()

    def test_position_supports_both_taskbar_edges_and_negative_monitors(self):
        cases = [
            ((1600, 1030, 1624, 1054), (520, 240), (0, 0, 1920, 1024)),
            ((20, 0, 44, 24), (520, 240), (0, 56, 1920, 1080)),
            ((-200, -100, -176, -76), (520, 240), (-3840, -2160, 0, 0)),
            ((-3830, -2100, -3806, -2076), (520, 240), (-3840, -2160, 0, 0)),
        ]
        for anchor, size, work in cases:
            with self.subTest(anchor=anchor):
                x, y = tooltip_position(anchor, size, work)
                self.assertGreaterEqual(x, work[0])
                self.assertGreaterEqual(y, work[1])
                self.assertLessEqual(x + size[0], work[2])
                self.assertLessEqual(y + size[1], work[3])

    def test_v4_mouse_and_keyboard_activation_dispatch_once(self):
        opened = Mock()
        icon = HoverIcon("hover-test", Image.new("RGBA", (16, 16)),
            menu=pystray.Menu(pystray.MenuItem("Open", opened, default=True)))
        icon._version4 = True
        with patch.object(icon, "update_menu"):
            for event in (0x0202, 0x0400, 0x0202, 0x0401):
                icon._on_notify(0, (37 << 16) | event)
        self.assertEqual(opened.call_count, 2)

    def test_v4_context_menu_maps_to_existing_pystray_handler(self):
        icon = HoverIcon("hover-menu-test", Image.new("RGBA", (16, 16)))
        icon._version4 = True
        with patch.object(pystray.Icon, "_on_notify") as legacy:
            icon._on_notify(0, (37 << 16) | 0x007B)
            legacy.assert_called_once_with(0, 0x0205)

    def test_context_menu_opens_settings_without_native_menu_overlay(self):
        for version, event in ((True, (37 << 16) | 0x007B), (False, 0x0205)):
            icon = HoverIcon("settings-test", Image.new("RGBA", (16, 16)))
            icon._version4 = version
            icon.settings_action = Mock()
            with patch.object(pystray.Icon, "_on_notify") as legacy:
                icon._on_notify(0, event)
            icon.settings_action.assert_called_once_with()
            legacy.assert_not_called()
            self.assertTrue(icon.suppress_hover)
            self.assertFalse(icon.hover_open)

    def test_shell_hover_events_disambiguate_shared_overflow_rectangles(self):
        icon = HoverIcon("hover-events-test", Image.new("RGBA", (16, 16)))
        icon._version4 = True
        icon._on_notify(0, (37 << 16) | 0x0406)
        self.assertTrue(icon.hover_open)
        popup = Mock()
        hover = HoverDetails({}, lambda _: "Grok details", threading.Event())
        hover.update(popup, "grok", (10, 10, 34, 34), 10, dwell=0)
        popup.show.assert_called_once_with("Grok details", (10, 10, 34, 34))
        icon._on_notify(0, (37 << 16) | 0x0407)
        self.assertFalse(icon.hover_open)

    def test_correct_native_id_is_sent_when_registering_and_querying(self):
        icon = HoverIcon("hover-id-test", Image.new("RGBA", (16, 16)))
        icon._hwnd = 123
        with patch("hover_details.win32.Shell_NotifyIcon", return_value=True) as send:
            self.assertTrue(icon._message(4, 0, uVersion=4))
        args = send.call_args.args
        self.assertEqual(args[1].uID, icon.icon_id)
        self.assertEqual(args[1].uVersion, 4)
        icon._hwnd = None

    def test_icon_the_shell_dropped_is_added_again(self):
        icon = HoverIcon("hover-reregister-test", Image.new("RGBA", (16, 16)))
        icon._hwnd = 123
        with patch.object(HoverIcon, "visible", True), patch.object(icon, "_show") as show, \
                patch.object(icon, "_message", return_value=True):
            self.assertFalse(icon.ensure_registered())   # NIM_MODIFY succeeds: still in the tray
            show.assert_not_called()
        with patch.object(HoverIcon, "visible", True), patch.object(icon, "_show") as show, \
                patch.object(icon, "_message", return_value=False):
            self.assertTrue(icon.ensure_registered())
            show.assert_called_once()
        icon._hwnd = None
        tray = QuotaTray.__new__(QuotaTray)
        tray.slots = [Mock(ensure_registered=Mock(return_value=False)), Mock(ensure_registered=Mock(return_value=True))]
        with self.assertLogs(tray_app.log, "WARNING"):
            tray.ensure_icons_registered()
        for slot in tray.slots:
            slot.ensure_registered.assert_called_once()

    def test_a_picture_the_shell_refused_is_sent_again(self):
        icon = HoverIcon("hover-resend-test", Image.new("RGBA", (16, 16)))
        icon._hwnd = 123
        with patch.object(icon, "_message", return_value=False), patch.object(icon, "_assert_icon_handle"), \
                self.assertLogs("hover_details", "INFO"):
            icon._update_icon()                        # Explorer busy: NIM_MODIFY fails
        self.assertTrue(icon.icon_failed)
        with patch.object(HoverIcon, "visible", True), patch.object(icon, "_message", return_value=True) as send, \
                patch.object(icon, "_assert_icon_handle"):
            self.assertTrue(icon.resend_icon())
        self.assertFalse(icon.icon_failed)
        import hover_details
        self.assertEqual(send.call_args.args[0], hover_details.win32.NIM_MODIFY)
        icon._hwnd = None
        # The tray assigns the same picture again while the last one was refused...
        tray = QuotaTray.__new__(QuotaTray)
        tray.ui_lock = threading.RLock()
        same = Image.new("RGBA", (16, 16))
        tray.snapshots, tray.performance = {"codex": None}, {"providers": {"codex": {}}}
        tray.tooltip = lambda provider: "Codex"
        tray.reset_level = lambda: "low"
        assigned = []
        slot_type = type("Slot", (), {"icon": property(lambda s: same, lambda s, value: assigned.append(value))})
        live = slot_type()
        live.icon_failed, live.title, live.details_visible = True, "", False
        tray.icons = {"codex": live}
        with patch.object(tray_app, "make_display_icon", return_value=same):
            tray.update_icon("codex", "C")
            live.icon_failed = False
            tray.update_icon("codex", "C")             # ...but not when the Shell took it
        self.assertEqual(assigned, [same])
        # ...and every slot's picture is sent again on the 30 s check, except one under the pointer.
        tray.slots = [Mock(ensure_registered=Mock(return_value=False), details_visible=False),
                      Mock(ensure_registered=Mock(return_value=False), details_visible=True)]
        tray.ensure_icons_registered()
        tray.slots[0].resend_icon.assert_called_once()
        tray.slots[1].resend_icon.assert_not_called()

    def test_unchanged_pixels_do_not_rebuild_native_icons(self):
        tray = QuotaTray.__new__(QuotaTray)
        image = Image.new("RGBA", (64, 64), "green")
        icon = Mock()
        icon.details_visible = icon.icon_failed = False
        icon.icon = image
        tray.icons = {"codex": icon}
        tray.snapshots = {"codex": None}
        tray.performance = {"providers": {"codex": {}}}
        tray.tooltip = Mock(return_value="details")
        with patch("tray_app.make_display_icon", return_value=image.copy()):
            tray.update_icon("codex", "C")
        self.assertIs(icon.icon, image)
        with patch("tray_app.make_display_icon", return_value=Image.new("RGBA", (64, 64), "red")):
            tray.update_icon("codex", "C")
        self.assertNotEqual(icon.icon, image)

    def test_panel_stays_after_switching_to_another_icon(self):
        icons = {"codex": SimpleNamespace(_version4=True, hover_open=False), "grok": SimpleNamespace(_version4=True, hover_open=True)}
        agents = Mock()
        hover = HoverDetails(icons, lambda _: "", threading.Event(), agents)
        hover.candidate = "codex"
        panel = Mock(provider="codex", closing_at=None)
        panel.scene = SimpleNamespace(dragging=False, press=False)
        panel.contains.return_value = False
        panel.frame.return_value = []
        rect = (10, 10, 34, 66)
        hover.drive_panel(panel, wintypes.POINT(20, 30), "grok", rect, False, False, 1.0)
        icons["grok"].hover_open = False      # Shell POPUPCLOSE a few seconds later
        with patch("hover_details.icon_rect", side_effect=lambda icon: rect if icon is icons["grok"] else None):
            self.assertEqual(("grok", rect), hover.target_at(wintypes.POINT(20, 30)))

    def test_shell_popup_close_during_refresh_does_not_close_our_hover(self):
        icon = SimpleNamespace(_version4=True, hover_open=False)
        hover = HoverDetails({"codex": icon}, lambda _: "live", threading.Event())
        rect = (10, 10, 34, 66)
        with patch("hover_details.icon_rect", return_value=rect):
            self.assertEqual(hover.target_at(wintypes.POINT(20, 30)), (None, None))
            hover.candidate = "codex"
            self.assertEqual(hover.target_at(wintypes.POINT(20, 30)), ("codex", rect))
            self.assertEqual(hover.target_at(wintypes.POINT(40, 30)), (None, None))

    def test_an_icon_keeps_updating_while_its_panel_is_open(self):
        tray = QuotaTray.__new__(QuotaTray)
        ring = Image.new("RGBA", (64, 64), "green")
        icon = SimpleNamespace(icon=ring, title="最近TPS 暂无完整计数", details_visible=True, icon_failed=False)
        tray.icons, tray.snapshots = {"codex": icon}, {"codex": None}
        tray.performance = {"providers": {"codex": {"active_sessions": 1, "display_tps": 200.0}}}
        tray.tooltip = Mock(return_value="最近TPS 200.0")
        tps = Image.new("RGBA", (64, 64), "red")
        with patch("tray_app.make_display_icon", return_value=tps):
            tray.update_icon("codex", "C")                 # generation started while the panel was open
        self.assertIs(icon.icon, tps)
        self.assertEqual(icon.title, "最近TPS 200.0")

    def test_steady_popup_does_not_repeat_showwindow_or_change_tracking_position(self):
        popup = TrackingTooltip.__new__(TrackingTooltip)
        popup.hwnd, popup.visible, popup.text = 123, True, "same text"
        popup.send = Mock()
        def window_rect(_hwnd, pointer):
            pointer._obj.left, pointer._obj.top = 20, 50
            pointer._obj.right, pointer._obj.bottom = 220, 150
            return True
        def monitor_info(_monitor, pointer):
            pointer._obj.rcWork = wintypes.RECT(0, 0, 1000, 1000)
            return True
        with patch("hover_details.user.GetWindowRect", side_effect=window_rect), \
             patch("hover_details.user.GetMonitorInfoW", side_effect=monitor_info), \
             patch("hover_details.user.MonitorFromRect", return_value=1), \
             patch("hover_details.is_window_covered", return_value=False), \
             patch("hover_details.user.IsWindowVisible", return_value=True), \
             patch("hover_details.user.SetWindowPos") as move:
            for _ in range(20):
                popup.show("same text", (200, 158, 220, 182))
        popup.send.assert_not_called()
        move.assert_not_called()

    def test_coverage_uses_zorder_even_for_static_windows_ignored_by_hit_testing(self):
        def fullscreen_rect(_hwnd, pointer):
            pointer._obj.left, pointer._obj.top = 0, 0
            pointer._obj.right, pointer._obj.bottom = 1920, 1080
            return True
        with patch("hover_details.user.GetWindow", side_effect=[456, 0]), \
             patch("hover_details.user.IsWindowVisible", return_value=True), \
             patch("hover_details.user.GetWindowLongW", return_value=0), \
             patch("hover_details.user.GetWindowRect", side_effect=fullscreen_rect):
            self.assertTrue(is_window_covered(123, wintypes.RECT(1120, 800, 1640, 1024)))
        with patch("hover_details.user.GetWindow", side_effect=[456, 0]), \
             patch("hover_details.user.IsWindowVisible", return_value=True), \
             patch("hover_details.user.GetWindowLongW", return_value=0x20):
            self.assertFalse(is_window_covered(123, wintypes.RECT(1120, 800, 1640, 1024)))

    def test_hover_details_include_data_beyond_shell_127_character_limit(self):
        tray = QuotaTray.__new__(QuotaTray)
        tray.snapshots = {"codex": QuotaSnapshot("codex", "Codex", 10080,
            13, 87, 1800000000, 1792132045, False, None)}
        tray.performance = {"providers": {"codex": {"label": "GPT / Codex",
            "active_sessions": 2, "generation_tps": 58.5, "output_tps_60": 12.0,
            "output_tokens_60": 720, "last_sample_at": 1792132045,
            "basis": ["客户端流式统计：真实输出计数，包含思考和工具输出"]}}}
        text = tray.hover_text("codex")
        for expected in ("58.5 tok/s", "720 输出tokens", "活动会话：2", "剩余 87%",
                         "来源：", "重置时间：", "距离重置：", "更新时间："):
            self.assertIn(expected, text)
        self.assertGreater(len(text), 127)


class FeatureIntegrationTests(unittest.TestCase):
    def tray(self):
        tray = QuotaTray.__new__(QuotaTray)
        tray.snapshots = {"codex": QuotaSnapshot("codex", "Codex", 300, 13, 87, 1800000000, 1792132045,
            False, None, 10080, 71, 29, 1800400000)}
        tray.performance = {"providers": {"codex": {"label": "GPT / Codex", "active_sessions": 1,
            "generation_tps": 58.5, "display_tps": 58.5, "output_tps_60": 12.0, "output_tokens_60": 720,
            "last_sample_at": 1792132045, "basis": ["客户端流式统计"]}}}
        return tray

    def test_weekly_countdown_uses_days(self):
        with patch("tray_app.time.time", return_value=0):
            self.assertEqual(reset_countdown(5 * 86400 + 3 * 3600 + 60), "5 天 3 小时")
            self.assertEqual(reset_countdown(2 * 3600 + 300), "2 小时 5 分")

    def test_startup_command_never_relies_on_file_association(self):
        with patch.object(tray_app.sys, "frozen", False, create=True):
            self.assertIn("-X utf8", startup_command())

    def test_finished_generation_wakes_refresh_worker(self):
        tray = self.tray()
        tray.scheduler = RefreshScheduler(["codex"], 0)
        tray.scheduler.take_due(0)
        tray.scheduler.completed("codex", tray.snapshots["codex"], 400)
        tray.wake = threading.Event()
        tray.last_samples = {"codex": 100.0}
        data = tray.performance["providers"]["codex"]
        data["last_sample_at"] = 200.0
        tray.feed_scheduler(tray.performance, 500)
        self.assertTrue(tray.wake.is_set())
        self.assertLessEqual(tray.scheduler.due["codex"], 510)

class IconArtTests(unittest.TestCase):
    def test_every_state_renders_a_64px_rgba_icon(self):
        from icon_art import live_icon, quota_icon
        images = [quota_icon(value, letter, stale) for letter in "CGM"
                  for value in (None, 0, 12, 42, 87, 100) for stale in (False, True)]
        images += [live_icon(value, letter) for letter in "CGM" for value in (0, 7, 58, 143, 2500, 99999)]
        for image in images:
            self.assertEqual((image.mode, image.size), ("RGBA", (64, 64)))
            self.assertIsNotNone(image.getbbox())

    def test_ring_length_and_colour_follow_remaining_quota(self):
        from icon_art import AMBER, CLAY, accent, quota_icon, ring_color
        self.assertEqual(ring_color(87, "C"), accent("C"))
        self.assertEqual(ring_color(42, "C"), AMBER)
        self.assertEqual(ring_color(12, "C"), CLAY)
        self.assertNotEqual(quota_icon(87, "C").tobytes(), quota_icon(42, "C").tobytes())
        # Stale data adds a badge without changing the quota reading.
        self.assertNotEqual(quota_icon(87, "C").tobytes(), quota_icon(87, "C", stale=True).tobytes())

    def test_large_tps_values_stay_short(self):
        from icon_art import tps_text
        self.assertEqual([tps_text(v) for v in (7.4, 143, 2500, 99999)], ["7", "143", "2k", "9k+"])


class SteadyStateCostTests(unittest.TestCase):
    def test_unchanged_icons_are_not_redrawn_every_second(self):
        snapshot = QuotaSnapshot(provider="codex", label="Codex", window_minutes=300, used_percent=40, remaining_percent=60,
                                 resets_at=None, updated_at=time.time(), stale=False, error=None)
        idle = {"active_sessions": 0, "display_tps": None}
        first = tray_app.make_display_icon(snapshot, "C", idle)
        self.assertIs(first, tray_app.make_display_icon(snapshot, "C", idle))
        live = {"active_sessions": 1, "display_tps": 85.04}
        self.assertIs(tray_app.make_display_icon(snapshot, "C", live), tray_app.make_display_icon(snapshot, "C", dict(live, display_tps=85.01)))

    def test_mimo_ledger_only_sees_new_observations(self):
        from collections import deque
        events = deque(SimpleNamespace(identity=str(i), provider="other") for i in range(5))
        monitor = SimpleNamespace(events=events, added_total=5)
        seen = []
        ledger = SimpleNamespace(ingest=lambda items: seen.append([e.identity for e in items]) or 0, save=lambda: None)
        tracker = SimpleNamespace(ledger=ledger, view=lambda now=None: None)
        app = SimpleNamespace(tps_monitor=monitor, mimo_ingested=0, mimo_view_at=time.time(),
                              snapshots={"mimo": SimpleNamespace(remaining_percent=None)})
        with patch.object(tray_app, "mimo_tracker", return_value=tracker):
            QuotaTray.update_mimo_locally(app, time.time())
            events.append(SimpleNamespace(identity="5", provider="mimo"))
            monitor.added_total = 6
            QuotaTray.update_mimo_locally(app, time.time())
            QuotaTray.update_mimo_locally(app, time.time())
        self.assertEqual([[str(i) for i in range(5)], ["5"]], seen)


if __name__ == "__main__":
    unittest.main()


class TrayOrderTests(unittest.TestCase):
    @staticmethod
    def snap(provider, remaining, reset, second=None, second_reset=None):
        from quota_sources import QuotaSnapshot
        return QuotaSnapshot(provider, provider, 300, None if remaining is None else 100 - remaining, remaining, reset,
                             1.0, False, None, 10080 if second is not None else None,
                             None if second is None else 100 - second, second, second_reset)

    def test_fixed_order_ignores_quota_and_exhaustion(self):
        import providers
        self.assertEqual(providers.tray_positions(["codex", "grok", "gemini", "claude", "mimo"]),
                         ["claude", "codex", "gemini", "grok", "mimo"])
        self.assertEqual(providers.tray_positions(["mimo", "gemini", "codex"]), ["codex", "gemini", "mimo"])
        tray = QuotaTray.__new__(QuotaTray)
        snapshots = {
            "codex": self.snap("codex", 80, 5000, second=40, second_reset=9000),
            "grok": self.snap("grok", 0, 1000),                                     # used up
            "gemini": self.snap("gemini", 50, 3000),
            "claude": self.snap("claude", 0, 100),                                  # used up
            "mimo": self.snap("mimo", 60, 99999, second=5, second_reset=2000),
        }
        tray.snapshots = snapshots
        with patch.object(tray_app, "PROVIDERS", {key: key[0].upper() for key in snapshots}):
            self.assertEqual(tray.wanted_order(), ["claude", "codex", "gemini", "grok", "mimo"])
            tray.snapshots = {**snapshots, "claude": self.snap("claude", 90, 50), "grok": self.snap("grok", 90, 10)}
            self.assertEqual(tray.wanted_order(), ["claude", "codex", "gemini", "grok", "mimo"])

    def test_icons_hidden_in_the_overflow_take_the_last_providers(self):
        tray = QuotaTray.__new__(QuotaTray)

        class FakeIcon:
            details_visible = hover_open = False
            def __init__(self, uid, left, top=1024):
                self.icon_id, self.left, self.top, self.menu = uid, left, top, None
        # As measured: slot 0 sits in the overflow (its rectangle is the ^ button,
        # left of the others); slot 4 is on the taskbar but past three other
        # programs' icons. Windows' record says which one is hidden.
        slots = [FakeIcon(0, 1593), FakeIcon(1, 1617), FakeIcon(2, 1641), FakeIcon(3, 1665), FakeIcon(4, 1761)]
        tray.slots = slots
        tray.icons = dict(zip(["claude", "codex", "gemini", "grok", "mimo"], slots))
        tray.shown_order, tray.reordered_at = [], 0.0
        tray.build_menu = lambda provider: provider
        tray.update_icon = Mock()
        rect = lambda icon: (icon.left, icon.top, icon.left + 24, icon.top + 56)
        visibility = {0: False, 1: True, 2: True, 3: True, 4: True}
        with patch.object(tray_app, "PROVIDERS", dict.fromkeys(tray.icons, "?")), \
                patch.object(tray_app.hover_details, "icon_rect", rect), \
                patch.object(tray_app.hover_details, "tray_visibility", return_value=visibility):
            self.assertEqual([s.icon_id for s in tray.slots_by_position()], [1, 2, 3, 4, 0])
            self.assertTrue(tray.maybe_reorder(1000.0))
            # Claude, Codex, Gemini and X stay on the taskbar in order; MiMo takes the hidden icon.
            self.assertEqual({p: i.icon_id for p, i in tray.icons.items()},
                             {"claude": 1, "codex": 2, "gemini": 3, "grok": 4, "mimo": 0})
            self.assertEqual(tray.hidden_slots, 1)
        # Without Windows' record: two of ours on the same spot are both in the overflow,
        # and an overflow rectangle above the taskbar does not turn the row vertical.
        slots[0].left, slots[0].top = 1621, 908
        slots[2].left, slots[2].top = 1621, 908
        with patch.object(tray_app.hover_details, "icon_rect", rect), \
                patch.object(tray_app.hover_details, "tray_visibility", return_value={}):
            self.assertEqual([s.icon_id for s in tray.slots_by_position()], [1, 3, 4, 0, 2])

    def test_each_tray_slot_registers_under_its_own_id(self):
        icon = HoverIcon("own-id-test", Image.new("RGBA", (16, 16)))
        icon._hwnd, icon.icon_id = 123, 3
        with patch("hover_details.win32.Shell_NotifyIcon", return_value=True) as send:
            icon._message(4, 0, uVersion=4)
        self.assertEqual(send.call_args.args[1].uID, 3)
        self.assertEqual(HoverIcon.icon_id, 0)                     # the class default is untouched
        icon._hwnd = None
        self.assertEqual(tray_app.hover_details.tray_visibility(), {})   # a source run reads no record

    def test_reorder_assigns_providers_to_icons_by_screen_position(self):
        tray = QuotaTray.__new__(QuotaTray)

        class FakeIcon:
            details_visible = hover_open = False
            def __init__(self, name, left):
                self.name, self.left, self.menu = name, left, None
        # Explorer shows the second-created icon on the left.
        slots = [FakeIcon("slot0", 200), FakeIcon("slot1", 100), FakeIcon("slot2", None)]
        tray.slots = slots
        tray.icons = {"codex": slots[0], "grok": slots[1], "gemini": slots[2]}
        tray.shown_order, tray.reordered_at = [], 0.0
        tray.build_menu = lambda provider: f"menu-{provider}"
        tray.update_icon = Mock()
        tray.snapshots = {"codex": self.snap("codex", 50, 900), "grok": self.snap("grok", 0, 100),
                          "gemini": self.snap("gemini", 50, 300)}
        rect = lambda icon: None if icon.left is None else (icon.left, 0, icon.left + 24, 24)
        with patch.object(tray_app, "PROVIDERS", {"codex": "C", "grok": "G", "gemini": "M"}), \
                patch.object(tray_app.hover_details, "icon_rect", rect):
            self.assertTrue(tray.maybe_reorder(1000.0))
            # Left to right on screen: slot1, slot0, then the unplaced slot2.
            self.assertEqual({p: i.name for p, i in tray.icons.items()}, {"codex": "slot1", "gemini": "slot0", "grok": "slot2"})
            self.assertEqual(slots[1].menu, "menu-codex")
            self.assertEqual(tray.shown_order, ["codex", "gemini", "grok"])
            self.assertFalse(tray.maybe_reorder(2000.0))          # already right: nothing changes
            tray.snapshots["codex"] = self.snap("codex", 0, 50)   # used up: position stays
            self.assertFalse(tray.maybe_reorder(3000.0))
            self.assertEqual(tray.icons["codex"].name, "slot1")
            slots[0].left, slots[1].left = 100, 200               # user dragged the icons
            tray.reordered_at = 3000.0
            self.assertFalse(tray.maybe_reorder(3001.0))          # too soon after the last change
            self.assertLessEqual(tray_app.REORDER_MIN_SECONDS, 5)   # a drag is undone within seconds
            slots[0].hover_open = True
            self.assertFalse(tray.maybe_reorder(4000.0))          # pointer is on an icon
            slots[0].hover_open = False
            self.assertTrue(tray.maybe_reorder(4000.0))
            self.assertEqual(tray.icons["codex"].name, "slot0")


class RefreshCadenceTests(unittest.TestCase):
    def tray(self):
        from refresh_scheduler import RefreshScheduler
        tray = QuotaTray.__new__(QuotaTray)
        tray.scheduler = RefreshScheduler(("codex", "grok"), 0)
        tray.scheduler.take_due(0)
        for provider in ("codex", "grok"):
            tray.scheduler.completed(provider, SimpleNamespace(error=None, resets_at=None, secondary_resets_at=None), 0)
        tray.icons = {"codex": SimpleNamespace(details_visible=False, hover_open=True),
                      "grok": SimpleNamespace(details_visible=False, hover_open=False)}
        tray.performance = {"providers": {"codex": {}, "grok": {}}}
        tray.agents = [{"provider": "grok", "live": True}]
        tray.snapshots = {"codex": object(), "grok": object()}
        tray.last_samples = {}
        tray.wake = threading.Event()
        return tray

    def test_pointer_on_an_icon_or_panel_sets_the_hover_cadence_and_live_agents_count_as_use(self):
        tray = self.tray()
        tray.feed_scheduler(tray.performance, 100)
        self.assertTrue(tray.scheduler.hovered["codex"])
        self.assertEqual((tray.scheduler.due["codex"], tray.scheduler.reason["codex"]), (100, "悬停"))
        self.assertTrue(tray.scheduler.active["grok"])            # a live grok agent, not only generation
        self.assertTrue(tray.scheduler.hovered["grok"])           # in use, so its curve is in the open panel
        self.assertEqual(tray_app.quota_sources.WATCHED, {"codex", "grok"})
        self.assertEqual(tray_app.quota_sources.HOVER_STARTED["codex"], 100)     # when this look began
        self.assertTrue(tray.wake.is_set())
        tray.icons["codex"].hover_open, tray.icons["grok"].details_visible = False, True   # panel open on Grok
        tray.feed_scheduler(tray.performance, 200)
        self.assertEqual((tray.scheduler.hovered["codex"], tray.scheduler.hovered["grok"]), (False, True))

    def test_a_failing_read_never_leaves_its_source_stuck_running(self):
        tray = self.tray()
        tray.scheduler.request(["grok"], 50)
        self.assertEqual(tray.scheduler.take_due(50), ["grok"])
        with patch.object(tray, "refresh_providers", side_effect=RuntimeError("boom")):
            tray.refresh_one("grok")
        self.assertNotIn("grok", tray.scheduler.running)
        self.assertEqual(tray.scheduler.failures["grok"], 1)
        self.assertTrue(tray.wake.is_set())


class CodexLocalLimitTests(unittest.TestCase):
    def test_status_line_behind_the_shown_quota_is_ignored_on_the_tps_loop(self):
        now = time.time()
        old = QuotaSnapshot("claude", "Claude Code", 300, 80, 20, int(now + 3600), now - 60,
                            False, None, 10080, 83, 17, int(now + 7200))
        behind = QuotaSnapshot("claude", "Claude Code", 300, 30, 70, old.resets_at, now,
                               False, None, 10080, 71, 29, old.secondary_resets_at)    # an idle session's old figure
        ahead = QuotaSnapshot("claude", "Claude Code", 300, 82, 18, old.resets_at, now + 2,
                              False, None, 10080, 84, 16, old.secondary_resets_at)
        tray = QuotaTray.__new__(QuotaTray)
        tray.snapshots = {"claude": old}
        tray.history = None
        with patch("claude_status.latest", return_value={"updated_at": now, "observed_at": now}), \
                patch.object(tray_app, "get_claude_quota") as usage, \
                patch.object(tray_app.quota_sources, "native_claude_quota", return_value=behind):
            tray.update_claude_locally(now)
            self.assertIs(tray.snapshots["claude"], old)                     # not taken, not marked doubtful
            with patch.object(tray_app.quota_sources, "native_claude_quota", return_value=ahead), \
                    patch("claude_status.latest", return_value={"updated_at": now + 2, "observed_at": now + 2}):
                tray.update_claude_locally(now + 2)
        usage.assert_not_called()                                            # never the 7 s /usage on this loop
        self.assertEqual(tray.snapshots["claude"].remaining_percent, 18)

    def test_claude_reset_notice_requires_at_least_ninety_five_percent_remaining(self):
        now = time.time()
        old = QuotaSnapshot("claude", "Claude Code", 300, 80, 20, int(now + 3600), now - 60,
                            False, None, 10080, 83, 17, int(now + 7200))
        partial = QuotaSnapshot("claude", "Claude Code", 300, 30, 70, int(now + 5 * 3600), now,
                                False, None, 10080, 71, 29, int(now + 7 * 86400))
        self.assertEqual(tray_app.reset_changes(old, partial, now), [])
        full = QuotaSnapshot("claude", "Claude Code", 300, 3, 97, int(now + 5 * 3600), now,
                             False, None, 10080, 83, 17, int(now + 7200))
        self.assertEqual(len(tray_app.reset_changes(old, full, now)), 1)

    def test_session_record_replaces_older_snapshot_and_limit_error_requests_refresh(self):
        tray = QuotaTray.__new__(QuotaTray)
        old = QuotaSnapshot("codex", "Codex", 300, 20.0, 80.0, 5000, 100.0, False, None)
        tray.snapshots = {"codex": old}
        tray.history = None
        tray.update_ui = Mock()
        tray.scheduler = Mock()
        tray.wake = threading.Event()
        limits = {"primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": 9000}}
        tray.tps_monitor = SimpleNamespace(codex_rate_limits=(200.0, limits), codex_limit_hit_at=0.0)
        tray.update_codex_locally(200.0 + 6 * 3600 + 1)
        self.assertIs(tray.snapshots["codex"], old)   # records older than 6 hours are ignored
        tray.update_codex_locally(201.0)
        self.assertEqual(tray.snapshots["codex"].remaining_percent, 0.0)
        tray.update_ui.assert_called_once()
        tray.update_codex_locally(202.0)              # same record: no second update
        tray.update_ui.assert_called_once()
        tray.scheduler.request.assert_not_called()
        tray.tps_monitor.codex_limit_hit_at = 205.0
        tray.update_codex_locally(206.0)
        tray.scheduler.request.assert_called_once_with(["codex"], 209.0, "额度用尽")
        self.assertTrue(tray.wake.is_set())
        tray.update_codex_locally(207.0)
        tray.scheduler.request.assert_called_once()


class AgentPanelTests(unittest.TestCase):
    def test_burn_chart_prefers_five_hour_even_when_weekly_is_more_urgent(self):
        from pathlib import Path
        from forecast import QuotaHistory
        now = 1_800_000_000.0
        history = QuotaHistory(Path(self.folder.name) / "burn.json")
        five_reset, week_reset = int(now + 2 * 3600), int(now + 4 * 86400)
        for index in range(13):                          # one reading every 10 minutes for 2 hours
            at = now - 7200 + index * 600
            history.add("codex", at, 10 + index, five_reset)                 # 5 h: slow, 78 % left
            history.add("codex:secondary", at, 60 + 2.5 * index, week_reset)  # weekly: fast, 10 % left
        snapshot = QuotaSnapshot("codex", "Codex", 300, 22, 78, five_reset, now, False, None, 10080, 90, 10, week_reset)
        self.tray.snapshots["codex"], self.tray.history = snapshot, history
        chart = self.tray.burn_chart("codex", now)
        self.assertEqual([(s.label, s.slot) for s in chart.series], [("Codex · 5 小时", 0)])
        primary = chart.series[0]
        self.assertAlmostEqual(primary.points[-1][1], 78, delta=0.01)
        self.assertAlmostEqual(primary.rate, 6.0, delta=0.5)
        self.assertEqual(agent_panel.burn_points(primary, chart)[0][0][0], chart.start)   # enters at the left edge
        self.assertGreater(chart.end, now)                                   # room for the projection
        self.assertLess(chart.end, week_reset)                               # not squeezed into a week-long axis
        self.assertNotIn('每周', primary.label)
        self.tray.history = None
        self.assertIsNone(self.tray.burn_chart("codex", now))

    def test_chart_shows_the_live_window_from_its_start_without_zooming_out_to_far_forecasts(self):
        from pathlib import Path
        from forecast import QuotaHistory
        now = time.time()
        reset = int(now+3*3600)
        history = QuotaHistory(Path(self.folder.name) / 'recent-axis.json')
        for age, used in ((86400,1),(2400,10),(1200,11),(0,12)):
            history.add('codex',now-age,used,reset)
        self.tray.history = history
        self.tray.snapshots['codex'] = QuotaSnapshot('codex','Codex',300,12,88,reset,now,False,None)
        chart = self.tray.burn_chart('codex',now)
        points = chart.series[0].points
        begun = reset - 5 * 3600                                            # this 5-hour window began 2 h ago
        self.assertEqual(points[0], (round(begun, 1), 100.0))               # unused at its start: the curve begins there
        self.assertEqual([round(now - t) for t, _ in points[1:]], [2400, 1200, 0])   # every reading in the window stays
        self.assertGreaterEqual(chart.start, begun - 0.1)                   # yesterday's reading is another window
        self.assertLessEqual(chart.end, now + 0.6 * (now - begun) + 1)      # projection stays scaled to that span
        self.assertEqual(points[-1][1], 88)

    def test_chart_keeps_the_curve_from_before_a_reset_and_a_restart(self):
        from pathlib import Path
        from forecast import QuotaHistory
        now = time.time()
        old_reset, new_reset = int(now - 3600), int(now + 4 * 3600 + 1200)   # reset an hour ago; used again 40 min ago
        path = Path(self.folder.name) / 'across-reset.json'
        history = QuotaHistory(path)
        for ago, used in ((270 * 60, 20), (260 * 60, 45), (250 * 60, 70)):    # heavy use 4 h ago
            history.add('claude', now - ago, used, old_reset)
        for ago, used in ((1800, 1), (0, 2)):                                    # barely used since the reset
            history.add('claude', now - ago, used, new_reset)
        history.save()
        self.tray.history = QuotaHistory(path)                                  # as after a restart
        self.tray.snapshots['claude'] = QuotaSnapshot('claude', 'Claude Code', 300, 2, 98, new_reset, now, False, None)
        self.tray.performance['providers']['claude'] = {'active_sessions': 1}
        chart = self.tray.burn_chart('claude', now)
        points = next(s for s in chart.series if s.label.startswith('Claude')).points
        self.assertEqual([(round((now - t) / 60), r) for t, r in points],      # (minutes ago, % left)
                         [(270, 80), (260, 55), (250, 30),                       # the earlier window, as recorded
                          (60, 30), (60, 100),                                   # its reset steps back up to 100 %
                          (40, 100),                                             # the new window began unused
                          (30, 99), (0, 98)])
        self.assertEqual(chart.now - chart.start, 5 * 3600)                    # little use lately: the long view

    def test_chart_zooms_to_how_fast_quota_drops(self):
        now = 1_800_000_000.0

        def drops(every, step, minutes, start=50.0):
            count = int(minutes * 60 / every)
            return [(now - (count - index) * every, start - step * index) for index in range(count + 1)]
        self.assertEqual(agent_panel.burn_span([drops(30, 1, 10)], now), 10)           # 2 %/min: the close-up
        self.assertEqual(agent_panel.burn_span([drops(300, 1, 120)], now), 30)         # 1 % per 5 min
        self.assertEqual(agent_panel.burn_span([drops(1800, 1, 240)], now), 120)       # 1 % per half hour
        idle = [(now - 1200 + 60 * index, 70.0) for index in range(21)]
        self.assertEqual(agent_panel.burn_span([idle], now), 30)                       # no use: all 20 min of it
        self.assertEqual(agent_panel.burn_span([idle, drops(30, 1, 10)], now), 10)     # the busiest source decides
        reset = [(now - 600, 20.0), (now - 300, 100.0), (now - 60, 100.0)]
        self.assertEqual(agent_panel.burn_span([reset], now), 10)                      # a reset is not use
        steady = drops(150, 1, 30)                                                     # 2 % per 5 min, 4 per 10
        self.assertEqual(agent_panel.burn_span([steady], now), 10)
        self.assertEqual(agent_panel.burn_span([steady], now, previous=30), 30)        # neighbour on screen: kept
        self.assertEqual(agent_panel.burn_span([steady], now, previous=120), 10)       # two steps off: moves

    def test_chart_clock_labels_are_round_times_before_now(self):
        now = time.mktime((2026, 11, 1, 14, 8, 20, 0, 0, -1))
        Chart = agent_panel.BurnChart
        clock = lambda chart: [time.strftime('%H:%M', time.localtime(t)) for t in agent_panel.burn_time_ticks(chart)]  # noqa: E731
        self.assertEqual(clock(Chart(now - 1800, now + 450, now, ())), ['13:40', '13:45', '13:50', '13:55', '14:00', '14:05'])
        self.assertEqual(clock(Chart(now - 18000, now + 4500, now, ())), ['10:00', '11:00', '12:00', '13:00'])   # 14:00 would crowd "now"
        self.assertEqual(clock(Chart(now - 600, now + 150, now, ())), ['14:00', '14:02', '14:04', '14:06'])  # 14:08 is "now"

    def test_hover_zooms_at_once_then_out_to_two_hours_and_five(self):
        now = 1_800_000_000.0
        close = agent_panel.BurnChart(now - 600, now + 150, now, ())
        span = lambda rested: round((agent_panel.burn_dwell(close, rested).now - agent_panel.burn_dwell(close, rested).start) / 60, 2)  # noqa: E731
        (to_30, delay1, ease1), (to_2h, delay2, ease2), (to_5h, delay3, ease3) = agent_panel.BURN_DWELL_BEATS
        self.assertEqual((to_30, delay1, to_2h, to_5h), (30, 0.0, 120, 300))
        at_30 = delay1 + ease1
        at_2h = at_30 + delay2 + ease2
        at_5h = at_2h + delay3 + ease3
        middle = lambda origin, target: round(origin * (target / origin) ** 0.5, 2)  # noqa: E731
        self.assertEqual(span(0), 10)                                        # opens on the close-up
        self.assertEqual(span(ease1 / 2), middle(10, 30))                    # and is already moving: no lead-in wait
        self.assertEqual(span(at_30), 30)
        self.assertEqual(span(at_30 + delay2), 30)                           # holds there through the rest
        self.assertEqual(span(at_30 + delay2 + ease2 / 2), middle(30, 120))
        self.assertEqual(span(at_2h), 120)
        self.assertEqual(span(at_2h + delay3), 120)                          # holds 2 h, then the second zoom
        self.assertEqual(span(at_2h + delay3 + ease3 / 2), middle(120, 300))
        self.assertEqual(span(at_5h), 300)
        self.assertEqual(span(60), 300)
        for start, ease in ((delay1, ease1), (at_30 + delay2, ease2), (at_2h + delay3, ease3)):
            steps = [span(start + ease * i / 40) for i in range(41)]
            self.assertEqual(steps, sorted(steps))                           # only ever widens
            ratios = [b / a for a, b in zip(steps, steps[1:])]
            self.assertLess(ratios[0], ratios[20])                           # starts gently...
            self.assertLess(ratios[-1], ratios[20])                          # ...and settles gently
            self.assertLess(ratios[1] - ratios[0], .002)                     # no jolt: speed builds smoothly
        series = agent_panel.BurnSeries('Codex', 0, tuple((now - 1800 + 60 * i, 95 - (i if i < 25 else 25 + 3 * (i - 25)))
                                                          for i in range(31)), 40.0, now + 3600, 'C')
        close_up = agent_panel.BurnChart(now - 600, now + 150, now, (series,))
        near = agent_panel.burn_axis(close_up)
        far = agent_panel.burn_axis(agent_panel.burn_view(close_up, 30))
        self.assertNotEqual(near, far)                                       # the two views pick different scales
        first = agent_panel.burn_dwell(close_up, ease1 * .02).axis
        halfway = agent_panel.burn_dwell(close_up, ease1 / 2).axis
        self.assertAlmostEqual(first.top, near.top, delta=abs(far.top - near.top) * .01 + 1e-9)   # no jump at the start
        self.assertTrue(min(near.top, far.top) <= halfway.top <= max(near.top, far.top))
        self.assertIsNone(agent_panel.burn_dwell(close_up, at_30).axis)      # at rest: the view's own scale
        self.assertEqual(agent_panel.burn_sprite(agent_panel.burn_dwell(close_up, ease1 / 2),
                                                 (agent_panel.accent('C'),), 1.0).height, agent_panel.CHART_H)
        held = agent_panel.burn_dwell(close, at_30)
        two_hours = agent_panel.burn_dwell(close, at_2h)
        five = agent_panel.burn_dwell(close, at_5h)
        self.assertAlmostEqual(held.end - held.now, 180)                     # 30 min keeps a narrow forecast strip
        self.assertAlmostEqual(two_hours.end - two_hours.now, 720)           # 2 h gives the forecast its tenth
        self.assertAlmostEqual(five.end - five.now, 1800)                    # and so does 5 h
        already = agent_panel.BurnChart(now - 5 * 3600, now + 1800, now, ())
        self.assertIs(agent_panel.burn_dwell(already, 60), already)          # already 5 h: unchanged
        opened_half = agent_panel.BurnChart(now - 1800, now + 180, now, ())  # opens at 30 min: the 2 h zoom is already moving
        half = lambda rested: round((agent_panel.burn_dwell(opened_half, rested).now - agent_panel.burn_dwell(opened_half, rested).start) / 60, 2)  # noqa: E731
        self.assertEqual(half(0), 30)
        self.assertEqual(half(ease2 / 2), middle(30, 120))
        self.assertEqual(half(ease2), 120)
        self.assertEqual(half(ease2 + delay3), 120)                          # rests, then the 5 h zoom
        self.assertEqual(half(ease2 + delay3 + ease3), 300)
        self.assertTrue(agent_panel.dwell_easing(30, ease2 * 0.02))
        self.assertFalse(agent_panel.dwell_easing(30, ease2 + delay3 / 2))
        opened_wide = agent_panel.BurnChart(now - 7200, now + 720, now, ())
        self.assertIs(agent_panel.burn_dwell(opened_wide, 0), opened_wide)   # 2 h starts the last zoom at once
        wide = lambda rested: round((agent_panel.burn_dwell(opened_wide, rested).now - agent_panel.burn_dwell(opened_wide, rested).start) / 60, 2)  # noqa: E731
        self.assertEqual(wide(ease3 / 2), middle(120, 300))
        self.assertEqual(agent_panel.burn_dwell(opened_wide, ease3).now
                         - agent_panel.burn_dwell(opened_wide, ease3).start, 5 * 3600)
        self.assertTrue(agent_panel.dwell_easing(120, ease3 * 0.02))
        self.assertFalse(agent_panel.dwell_easing(120, ease3 + 0.1))
        self.assertIsNone(agent_panel.burn_dwell(None, 60))
        self.assertTrue(agent_panel.dwell_easing(10, ease1 / 2))
        self.assertFalse(agent_panel.dwell_easing(10, at_30 + delay2 / 2))   # holding 30 min is still
        self.assertTrue(agent_panel.dwell_easing(10, at_30 + delay2 + ease2 / 2))
        self.assertFalse(agent_panel.dwell_easing(10, at_2h + delay3 / 2))   # holding 2 h is still
        self.assertTrue(agent_panel.dwell_easing(10, at_2h + delay3 + ease3 / 2))
        self.assertFalse(agent_panel.dwell_easing(10, at_5h + 1))
        self.assertFalse(agent_panel.dwell_easing(300, 60))
        early = agent_panel.burn_dwell(close, ease1 * 0.25)
        ticks = [time.strftime('%M', time.localtime(t)) for t in agent_panel.burn_time_ticks(early)]
        self.assertTrue(ticks and all(int(m) % 5 == 0 for m in ticks))       # still inside 30 min: its 5-min steps
        second = agent_panel.burn_dwell(close, at_30 + delay2 + ease2 / 2)   # exactly 60 min
        stamps = agent_panel.burn_time_ticks(second)
        self.assertGreaterEqual(len(stamps), 2)
        self.assertEqual(round(stamps[1] - stamps[0]), 15 * 60)
        third = agent_panel.burn_dwell(close, at_2h + delay3 + ease3 / 2)    # between 2 h and 5 h: hourly steps
        hours = agent_panel.burn_time_ticks(third)
        self.assertGreaterEqual(len(hours), 2)
        self.assertEqual(round(hours[1] - hours[0]), 3600)

    def test_hover_keeps_five_hours_of_readings_for_the_last_zoom(self):
        from pathlib import Path
        from forecast import QuotaHistory
        now = 1_800_000_000.0
        reset = int(now + 4 * 3600)                                         # the live window began an hour ago
        history = QuotaHistory(Path(self.folder.name) / 'five-hour-zoom.json')
        history.add('codex', now - 6 * 3600, 1, reset)                      # older than the chart keeps
        history.add('codex', now - int(4.5 * 3600), 2, reset)               # inside the 5 h the last zoom shows
        for index in range(31):                                             # then 1 % every 20 s: a 10 min close-up
            history.add('codex', now - 600 + index * 20, 10 + index, reset)
        self.tray.history = history
        self.tray.snapshots['codex'] = QuotaSnapshot('codex', 'Codex', 300, 40, 60, reset, now, False, None)
        chart = self.tray.burn_chart('codex', now)
        self.assertEqual(chart.now - chart.start, 600)                      # the picture still opens on 10 min
        kept = [round(t, 1) for t, _ in chart.series[0].points]
        self.assertNotIn(round(now - 6 * 3600, 1), kept)
        self.assertIn(round(now - 4.5 * 3600, 1), kept)
        widened = agent_panel.burn_dwell(chart, 60)
        self.assertEqual(widened.now - widened.start, 5 * 3600)
        drawn = agent_panel.burn_points(widened.series[0], widened)[0]
        self.assertTrue(any(abs(t - (now - 4.5 * 3600)) < 1 for t, _ in drawn))  # the 5 h view still has it

    def test_chart_axis_keeps_a_visible_short_term_forecast(self):
        from pathlib import Path
        from forecast import QuotaHistory
        now = time.time()
        reset = int(now + 4 * 3600)
        history = QuotaHistory(Path(self.folder.name) / 'close-up.json')
        for index in range(41):                                                  # 2 % a minute for 20 min
            history.add('codex', now - 1200 + index * 30, 10 + index, reset)
        self.tray.history = history
        self.tray.snapshots['codex'] = QuotaSnapshot('codex', 'Codex', 300, 50, 50, reset, now, False, None)
        chart = self.tray.burn_chart('codex', now)
        self.assertEqual((chart.now - chart.start, chart.end - chart.now), (600, 60))  # 10 min back, 1 min ahead
        history_points, projection, _ = agent_panel.burn_points(chart.series[0], chart)
        self.assertEqual(history_points[0][0], chart.start)                          # no gap at the left edge
        self.assertEqual(len(chart.series[0].points), 42)                            # all 20 min (hovering widens to 30) + the window start
        self.assertTrue(projection)
        sprite = agent_panel.burn_sprite(chart, (agent_panel.accent('C'),), 1.0)
        self.assertEqual(sprite.height, agent_panel.CHART_H)

    def test_one_off_glitches_leave_the_curve_but_real_resets_stay(self):
        from pathlib import Path
        import json
        from forecast import QuotaHistory, Sample, without_blips
        window, other = 1_800_000_000, 1_800_003_060
        claude = [Sample(0, 6, window), Sample(60, 0, window), Sample(70, 0, window),      # idle session: 100% twice
                  Sample(71, 7, window), Sample(80, 9, window), Sample(90, 0, window), Sample(91, 16, window)]
        self.assertEqual([s.at for s in without_blips(claude)], [0, 71, 80, 91])
        grok = [Sample(0, 55, window), Sample(600, 0, other), Sample(1200, 55, window), Sample(1800, 56, window)]
        self.assertEqual([s.at for s in without_blips(grok)], [0, 1200, 1800])          # another window, once
        reset = [Sample(0, 90, window), Sample(60, 0, other), Sample(120, 2, other)]
        self.assertEqual(without_blips(reset), reset)                                     # the new window stays
        jump = [Sample(0, 10, window), Sample(60, 20, window), Sample(120, 21, window)]
        self.assertEqual(without_blips(jump), jump)                                       # a burst of real use stays
        self.assertEqual(without_blips(claude[:2]), claude[:2])                           # the latest is not judged yet
        path = Path(self.folder.name) / 'blips.json'
        path.write_text(json.dumps({"series": {"grok": [[s.at, s.used, s.resets_at] for s in grok]}}), encoding="utf-8")
        loaded = QuotaHistory(path)
        self.assertEqual([s.at for s in loaded.series["grok"]], [0, 1200, 1800])        # cleaned on load
        loaded.save()
        self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))["series"]["grok"]), 3)

    def test_idle_sources_are_faint_context_on_one_legend_row(self):
        series = [agent_panel.BurnSeries('Claude Code · 5 小时', 0, ((0, 60.0), (600, 40.0)), 2.0, None, 'A'),
                  agent_panel.BurnSeries('Codex · 5 小时', 0, ((0, 100.0), (600, 100.0)), None, None, 'C', idle=True),
                  agent_panel.BurnSeries('Gemini · Argon', 0, ((0, 100.0),), None, None, 'G', True, idle=True),
                  agent_panel.BurnSeries('Gemini · Claude/GPT', 1, ((0, 0.0),), None, None, 'G', True, idle=True),
                  agent_panel.BurnSeries('Gemini · Gemini', 2, ((0, 100.0),), None, None, 'G', True, idle=True),
                  agent_panel.BurnSeries('Grok', 0, ((0, 44.0),), .7, None, 'X', idle=True),
                  agent_panel.BurnSeries('Xiaomi MiMo · 套餐额度', 0, ((0, 94.0),), 0.0, None, 'M', idle=True)]
        chart = agent_panel.BurnChart(0, 660, 600, tuple(series))
        legend = agent_panel.burn_legend(chart)
        self.assertEqual(legend[0][3], 'Claude Code · 5 小时')                        # in use first, full name
        self.assertTrue(all(row == 0 for _, _, row, _ in legend))                    # one row: no second for idle
        self.assertTrue({'Codex', 'Argon'} <= {text for *_, text in legend})          # idle ones by short name
        self.assertEqual(agent_panel.chart_extra(chart), 0)
        self.assertLess(agent_panel.burn_axis(chart).top, 100)                         # scale fits the line in use
        _, projection, _ = agent_panel.burn_points(series[5], chart)
        sprite = agent_panel.burn_sprite(chart, tuple(agent_panel.accent(s.letter) for s in series), 1.0)
        self.assertEqual(sprite.height, agent_panel.CHART_H)
        self.assertEqual(agent_panel.short_label('Xiaomi MiMo · 套餐额度'), 'Xiaomi MiMo')

    def test_quota_history_folds_repeated_readings_so_the_curve_is_kept(self):
        from pathlib import Path
        from forecast import KEEP_SAMPLES, QuotaHistory
        history = QuotaHistory(Path(self.folder.name) / 'folded.json')
        start = 1_000_000.0
        for index in range(5000):                       # a reading every 5 s for ~7 h, mostly unchanged
            history.add('claude', start + index * 5, 10 + index // 500, 9_999_999)
        samples = history.samples('claude')
        self.assertLess(len(samples), 40)                                  # two per level, not 5000
        self.assertLess(len(samples), KEEP_SAMPLES)
        self.assertEqual((samples[0].at, samples[-1].at), (start, start + 4999 * 5))   # the whole span is kept
        self.assertEqual([s.used for s in samples[:3]], [10, 10, 11])      # each level's first and latest reading

    def test_stale_source_names_when_and_why_in_the_panel(self):
        now = time.time()
        seen = now - 1800
        self.tray.snapshots['grok'] = QuotaSnapshot(
            'grok', 'Grok', 10080, 35, 65, int(now + 86400), seen, True,
            '读取 /usage 失败：RuntimeError: RuntimeError: Grok 官方额度服务出错，暂时读不到额度')
        text, tone = self.tray.headline_forecast('grok')
        clock = tray_app.datetime.fromtimestamp(seen).strftime('%H:%M')
        self.assertEqual((text, tone), (f'显示 {clock} 的读数；Grok 官方额度服务出错，暂时读不到额度', 'warn'))

    def test_stale_note_reasons(self):
        now = time.time()
        timeout = QuotaSnapshot('grok', 'Grok', 10080, 35, 65, None, None, True,
                                '读取 /usage 失败：RuntimeError: TimeoutError: Grok 会话在 25 秒内未返回 Weekly limit')
        self.assertEqual(tray_app.stale_note(timeout, now), '读取超时')
        quiet = QuotaSnapshot('claude', 'Claude Code', 300, 55, 45, None, now - 2 * 86400, True, None)
        self.assertRegex(tray_app.stale_note(quiet, now), r'^显示 \d\d-\d\d \d\d:\d\d 的读数；未能读取最新额度$')

    def test_exhausted_codex_switches_to_weekly_only_while_working(self):
        from pathlib import Path
        from forecast import QuotaHistory
        from tps_sources import Observation
        now = time.time()
        self.tray.history = QuotaHistory(Path(self.folder.name) / 'quota-mode.json')
        snapshot = QuotaSnapshot('codex', 'Codex', 300, 100, 0, int(now+3600), now, False, None,
                                 10080, 39, 61, int(now+86400))
        self.tray.snapshots['codex'] = snapshot
        self.assertTrue(self.tray.codex_quota_mode())
        chart = self.tray.burn_chart('claude', now)
        self.assertEqual([(s.label,s.slot,s.points[-1][1]) for s in chart.series],
                         [('Codex · 每周（5 小时已用完）',1,61)])
        self.assertEqual(self.tray.panel('codex').status, '额度模式工作')
        self.assertTrue(self.tray.headline_forecast('codex')[0].startswith('5 小时额度已用完，仍在工作'))
        snapshot.remaining_percent = 100
        self.assertFalse(self.tray.codex_quota_mode())
        self.assertEqual(self.tray.burn_chart('codex',now).series[0].label, 'Codex · 5 小时')
        snapshot.remaining_percent = 0
        self.tray.agents = []
        self.tray.tps_monitor.events = [Observation('done','Codex','session','codex','gpt-6.1',now-10,50)]
        self.assertFalse(self.tray.codex_quota_mode())
        self.assertEqual(self.tray.burn_chart('codex',now).series[0].label, 'Codex · 5 小时')
        self.tray.performance['providers']['codex']['active_sessions'] = 1
        self.assertTrue(self.tray.codex_quota_mode())
        snapshot.secondary_remaining_percent = 0
        self.assertTrue(self.tray.codex_quota_mode())
        snapshot.secondary_remaining_percent = None
        self.assertFalse(self.tray.codex_quota_mode())

    def test_claude_chart_uses_five_hour_instead_of_weekly(self):
        from pathlib import Path
        from forecast import QuotaHistory
        now = time.time()
        self.tray.history = QuotaHistory(Path(self.folder.name) / 'claude-window.json')
        self.tray.agents = [{'provider':'claude','live':True}]
        self.tray.snapshots['claude'] = QuotaSnapshot('claude','Claude Code',300,13,87,int(now+3600),now,False,None,
                                                     10080,2,98,int(now+86400))
        chart = self.tray.burn_chart('codex',now)
        self.assertEqual([s.label for s in chart.series], ['Claude Code · 5 小时'])

    def test_only_live_gemini_models_highlight_their_own_pool(self):
        from tps_sources import Observation
        now = time.time()
        reset = int(now+3600)
        pools = [{'key':'argon','label':'Argon','remaining_percent':60,'resets_at':reset,'window_minutes':300},
                 {'key':'claude_gpt','label':'Claude/GPT','remaining_percent':40,'resets_at':reset,'window_minutes':300},
                 {'key':'gemini','label':'Gemini','remaining_percent':70,'resets_at':reset,'window_minutes':300}]
        self.tray.snapshots['gemini'] = QuotaSnapshot('gemini','Gemini',300,40,60,reset,now,False,None,pools=pools)
        agent = dict(self.tray.agents[0],provider='gemini',live=True,model='gemini-3.8-flash')
        self.tray.agents = [agent]
        self.tray.tps_monitor.events = [Observation('past','Antigravity','old','gemini','claude-opus-5',now-20,50)]
        self.assertEqual(self.tray.panel('gemini').quota_tones, ('','','active'))

    def test_chart_with_little_history_shows_all_of_it(self):
        from pathlib import Path
        from forecast import QuotaHistory
        now = time.time()
        reset = int(now + 4 * 3600 + 2400)                       # the window began with the first reading
        history = QuotaHistory(Path(self.folder.name) / 'first-reading.json')
        for age, used in ((1200, 10), (600, 11), (0, 12)):      # recorded for 20 minutes, slowly
            history.add('codex', now - age, used, reset)
        self.tray.history = history
        self.tray.snapshots['codex'] = QuotaSnapshot('codex', 'Codex', 300, 12, 88, reset, now, False, None)
        chart = self.tray.burn_chart('codex', now)
        self.assertEqual(chart.now - chart.start, 30 * 60)                      # the shortest span holding it all
        self.assertLessEqual(chart.start, chart.series[0].points[0][0])

    def test_gemini_forecast_names_only_pools_running_out(self):
        from pathlib import Path
        from forecast import QuotaHistory
        now = 1_800_000_000.0
        reset = int(now + 3 * 86400)

        def record(argon_left, gemini_rate):
            history = QuotaHistory(Path(self.folder.name) / f"pool-forecast-{argon_left}-{gemini_rate}.json")
            for index in range(13):
                at = now - 7200 + index * 600
                argon = argon_left + 2 * (12 - index)
                pools = [{"key": "argon", "label": "Argon", "used_percent": 100 - argon, "remaining_percent": argon,
                          "resets_at": reset, "window_minutes": 10080},
                         {"key": "claude_gpt", "label": "Claude/GPT", "used_percent": 20.0, "remaining_percent": 80.0,
                          "resets_at": reset, "window_minutes": 10080},
                         {"key": "gemini", "label": "Gemini", "used_percent": 10 + gemini_rate * index,
                          "remaining_percent": 90 - gemini_rate * index, "resets_at": reset, "window_minutes": 10080}]
                snapshot = QuotaSnapshot("gemini", "Gemini", 10080, 100 - argon, argon, reset, at, False, None, pools=pools)
                history.record(snapshot)
            self.tray.snapshots["gemini"], self.tray.history = snapshot, history
            return self.tray.pool_forecast("gemini", now)

        text, tone = record(26, 1)        # Argon 12 %/h with 26 % left; Gemini 6 %/h with 78 % left
        self.assertEqual(text, "Argon 预计 2 小时后用完；Gemini 预计 13 小时后用完")
        self.assertEqual(tone, "warn")
        self.assertEqual(self.tray.headline_forecast("gemini"), self.tray.pool_forecast("gemini"))
        text, _ = record(5, 1)            # under 10 % left: its run-out time is not announced
        self.assertNotIn("Argon", text)
        self.assertIn("Gemini 预计", text)
        self.assertEqual(record(5, 0), ("额度够用", "ok"))
        self.tray.history = None
        self.assertEqual(self.tray.pool_forecast("gemini", now), ("", "unknown"))

    def test_mimo_and_grok_chart_labels_are_the_model_name_only(self):
        from pathlib import Path
        from forecast import QuotaHistory
        now = 1_800_000_000.0
        self.tray.history = QuotaHistory(Path(self.folder.name) / "short-names.json")
        self.tray.snapshots["mimo"] = QuotaSnapshot("mimo", "Xiaomi MiMo", 525600, 3, 97, int(now + 86400), now, False, None)
        self.tray.snapshots["grok"] = QuotaSnapshot("grok", "Grok", 10080, 35, 65, int(now + 86400), now, False, None)
        self.tray.agents = []
        chart = self.tray.burn_chart("codex", now)
        self.assertEqual([s.label for s in chart.series], ["Grok", "MiMo"])

    def test_every_gemini_pool_stays_on_the_chart(self):
        from pathlib import Path
        from forecast import QuotaHistory
        now = 1_800_000_000.0
        history = QuotaHistory(Path(self.folder.name) / "pools.json")
        reset = int(now + 3 * 86400)
        for index in range(13):
            at = now - 7200 + index * 600
            pools = [{"key": "argon", "label": "Argon", "used_percent": 50 + 2 * index, "remaining_percent": 50 - 2 * index,
                      "resets_at": reset, "window_minutes": 10080},
                     {"key": "claude_gpt", "label": "Claude/GPT", "used_percent": 20.0, "remaining_percent": 80.0,
                      "resets_at": reset, "window_minutes": 10080},
                     {"key": "gemini", "label": "Gemini", "used_percent": 10 + index, "remaining_percent": 90 - index,
                      "resets_at": reset, "window_minutes": 10080}]
            snapshot = QuotaSnapshot("gemini", "Gemini", 10080, pools[0]["used_percent"], pools[0]["remaining_percent"], reset,
                                     at, False, None, pools=pools)
            history.record(snapshot)
        self.tray.snapshots["gemini"], self.tray.history = snapshot, history
        self.tray.agents = [{"provider":"gemini", "live":True, "model":"argon"},
                            {"provider":"gemini", "live":True, "model":"gemini-3.8-flash"}]
        chart = self.tray.burn_chart("gemini", now)
        self.assertEqual([(s.label, s.slot) for s in chart.series],
                         [("Gemini · Argon", 0), ("Gemini · Claude/GPT", 1), ("Gemini", 2)])
        self.assertEqual([round(s.points[-1][1]) for s in chart.series], [26, 80, 78])
        with patch.object(self.tray, "agents", []):
            self.assertEqual([s.label for s in self.tray.burn_chart("gemini", now).series],
                             ["Gemini · Argon", "Gemini · Claude/GPT", "Gemini"])
        self.tray.agents = [{"provider":"gemini", "live":True, "model":"claude-opus-5-5"}]
        self.assertEqual([s.label for s in self.tray.burn_chart("codex", now).series],
                         ["Gemini · Argon", "Gemini · Claude/GPT", "Gemini"])
        # Restore normal bubble records before exercising the complete renderer.
        self.tray.agents = []
        model = self.tray.panel("gemini")
        self.assertEqual([q[2] for q in model.quota], ["Argon", "Claude/GPT", "Gemini"])
        self.assertEqual([s.label for s in model.burn.series], ["Gemini · Argon", "Gemini · Claude/GPT", "Gemini"])
        scene = agent_panel.Scene()
        scene.set_model(model, 0.0)
        self.assertIsNotNone(scene.render(1.25))

    def test_idle_source_stays_on_the_chart_on_its_five_hour_window(self):
        from pathlib import Path
        from forecast import QuotaHistory
        now = 1_800_000_000.0
        history = QuotaHistory(Path(self.folder.name) / "idle.json")
        reset = int(now + 3600)
        for index in range(13):
            history.add("codex", now - 7200 + index * 600, 30, reset)
            history.add("codex:secondary", now - 7200 + index * 600, 95, reset + 86400)
        self.tray.snapshots["codex"] = QuotaSnapshot("codex", "Codex", 300, 30, 70, reset, now, False, None, 10080, 95, 5, reset + 86400)
        self.tray.history = history
        self.tray.agents = []
        chart = self.tray.burn_chart("codex", now)
        self.assertEqual([s.label for s in chart.series], ["Codex · 5 小时"])   # idle, and still the 5 h window
        self.assertAlmostEqual(chart.series[0].points[-1][1], 70, delta=0.01)

    def test_every_model_with_a_reading_shares_one_chart(self):
        from pathlib import Path
        from forecast import QuotaHistory
        now = time.time()
        self.tray.history = QuotaHistory(Path(self.folder.name) / 'global.json')
        for provider in ('codex', 'claude', 'grok'):
            self.tray.snapshots[provider] = QuotaSnapshot(provider, provider, 300, 25, 75,
                                                         int(now + 3600), now, False, None)
        self.tray.agents = [{'provider':'codex', 'live':False}, {'provider':'claude', 'live':True}]
        self.tray.performance['providers']['grok']['active_sessions'] = 1
        first = self.tray.burn_chart('codex', now)
        self.assertEqual({s.letter for s in first.series}, {'A', 'C', 'X'})
        self.assertEqual(first, self.tray.burn_chart('gemini', now))
        self.tray.agents = []
        self.tray.performance['providers']['grok']['active_sessions'] = 0
        self.assertEqual({s.letter for s in self.tray.burn_chart('grok', now).series}, {'A', 'C', 'X'})

    def test_mimo_today_stat_uses_current_cache_and_never_yesterdays_total(self):
        from datetime import datetime
        snapshot = tray_app.placeholder("mimo")
        snapshot.today_used = 1.53e9
        snapshot.today_date = datetime.now().strftime("%Y-%m-%d")
        self.tray.snapshots["mimo"] = snapshot
        self.tray.headline_forecast = Mock(return_value=("", "unknown"))
        with patch("tray_app.mimo_tracker") as tracker:
            self.assertEqual(self.tray.panel("mimo").today_used, 1.53e9)
            tracker.assert_not_called()
            snapshot.today_date = "2000-01-01"
            tracker.return_value.view.return_value.today_used = 1234
            self.assertEqual(self.tray.panel("mimo").today_used, 1234)

    def test_claude_stays_on_the_chart_after_its_reply(self):
        from pathlib import Path
        from forecast import QuotaHistory
        from tps_sources import Observation
        now = time.time()
        self.tray.history = QuotaHistory(Path(self.folder.name) / 'completed.json')
        self.tray.snapshots['claude'] = QuotaSnapshot('claude', 'Claude Code', 300, 15, 85, int(now+3600), now, False, None)
        self.tray.agents = []
        self.tray.tps_monitor.events = [Observation('done', 'Claude Code', 'session', 'claude', 'claude-opus-5-5', now-1200, 571)]
        chart = self.tray.burn_chart('gemini', now)
        self.assertEqual([s.letter for s in chart.series], ['A'])
        self.assertEqual(chart, self.tray.burn_chart('claude', now))
        self.assertEqual([s.letter for s in self.tray.burn_chart('claude', now + 601).series], ['A'])

    def test_claude_stays_on_the_chart_without_a_recent_desktop_reading(self):
        from pathlib import Path
        from forecast import QuotaHistory
        now = time.time()
        self.tray.history = QuotaHistory(Path(self.folder.name) / 'desktop.json')
        self.tray.snapshots['claude'] = QuotaSnapshot('claude', 'Claude Code', 300, 15, 85, int(now+3600), now, False, None)
        self.tray.agents = []
        self.tray.tps_monitor.claude_desktop_last_use = now-20
        self.assertEqual([s.letter for s in self.tray.burn_chart('codex', now).series], ['A'])
        self.tray.tps_monitor.claude_desktop_last_use = 0
        self.assertEqual([s.letter for s in self.tray.burn_chart('claude', now).series], ['A'])

    def setUp(self):
        import tempfile
        from pathlib import Path
        import agent_tasks
        self.folder = tempfile.TemporaryDirectory()
        tray = QuotaTray.__new__(QuotaTray)
        tray.snapshots = {provider: tray_app.placeholder(provider) for provider in PROVIDERS}
        tray.performance = {"providers": {provider: {"label": provider, "display_tps": 42.0} for provider in PROVIDERS}}
        tray.agent_groups = agent_tasks.AgentGroups(Path(self.folder.name) / "groups.json")
        tray.summarizer = agent_tasks.Summarizer(None)
        tray.clusters = {}
        tray.tps_monitor = SimpleNamespace(schedules=[{"key": "Codex:日报", "title": "日报", "prompt": "整理今日日报", "last_at": 0.0, "next_at": 0.0}])

        def agent(key, provider, live=True, parent="", schedule=""):
            return {"key": key, "provider": provider, "source": "", "model": "", "title": f"任务{key}", "thread_title": "",
                    "request": "", "doing": "运行测试", "live": live, "at": 1.0, "parent": parent, "role": "", "folder": "",
                    "tps": None, "schedule": schedule}
        tray.agents = [agent("g1", "grok"), agent("c1", "codex"), agent("c2", "codex", parent="c1"), agent("c3", "codex", live=False),
                       agent("s1", "codex", schedule="origin:巡检")]
        self.tray = tray

    def tearDown(self):
        self.folder.cleanup()

    def test_panel_for_every_provider_same_layout_and_schedules_apart(self):
        idle = self.tray.panel("gemini")
        self.assertNotEqual(idle.status_tone, "live")
        self.assertEqual(len(idle.bubbles), 3)
        model = self.tray.panel("codex")
        self.assertEqual(model.status, "2 个在工作")
        self.assertNotIn("s1", [key for bubble in model.bubbles for key in bubble.keys])
        self.assertFalse(hasattr(model, "schedules"))
        self.assertIsInstance(model.forecast, str)
        self.assertEqual(model.tps, 42.0)
        self.assertEqual(sorted(bubble.focus for bubble in model.bubbles), [False, True, True])
        # Same bubbles, order and header height on every icon: the panel never changes size when switching.
        other = self.tray.panel("grok")
        self.assertEqual([b.key for b in other.bubbles], [b.key for b in model.bubbles])
        self.assertEqual(agent_panel.field_top(other), agent_panel.field_top(model))
        self.assertEqual((model.hint_now, model.hint_next), (other.hint_now, other.hint_next))
        self.assertEqual(other.tps, 42.0)
        group = next(bubble for bubble in model.bubbles if bubble.members)
        self.assertEqual(len(group.members), 2)
        self.assertTrue(model.quota)

    def test_gemini_pools_fill_the_panel_header(self):
        snapshot = tray_app.placeholder("gemini")
        snapshot.error, snapshot.remaining_percent, snapshot.used_percent = None, 25.0, 75.0
        snapshot.pools = [{"key": "argon", "label": "Argon", "remaining_percent": 25.0, "resets_at": int(time.time()) + 3600},
                          {"key": "claude_gpt", "label": "Claude/GPT", "remaining_percent": 40.0, "resets_at": None},
                          {"key": "gemini", "label": "Gemini", "remaining_percent": 90.0, "resets_at": int(time.time()) + 3 * 86400}]
        self.tray.snapshots["gemini"] = snapshot
        model = self.tray.panel("gemini")
        self.assertEqual([item[1] for item in model.quota], [25.0, 40.0, 90.0])
        self.assertIn("小时", model.quota[0][0])      # countdown on top, model name under the bar
        self.assertEqual(model.quota[0][2], "Argon")
        self.assertEqual(model.quota[1][:3:2], ("Claude/GPT", ""))   # no reset known: the name stays on top
        self.assertEqual((model.quota[2][0][-1], model.quota[2][2]), ("天", "Gemini"))
        self.assertIn("Claude/GPT 40%", tray_app.quota_line(snapshot))
        self.assertEqual(len(tray_app.pool_lines(snapshot)), 3)

    def test_codex_at_zero_shows_its_purchased_credits_instead_of_zero_percent(self):
        now = time.time()
        codex = tray_app.QuotaSnapshot("codex", "Codex", 300, 100.0, 0.0, int(now + 3600), now, False, None,
                                       10080, 40.0, 60.0, int(now + 86400), credits_balance=59773.36)
        self.tray.snapshots["codex"] = codex
        model = self.tray.panel("codex")
        self.assertEqual(model.figure_of(0), ("59.8", "K"))
        self.assertEqual((model.quota[0][2], model.tone_of(0)), ("购买额度", "paid"))
        self.assertIsNone(model.figure_of(1))                      # the weekly window keeps its %
        scene = agent_panel.Scene()
        scene.set_model(model, 0)
        self.assertIsNotNone(scene.render(1.25))
        codex.credits_balance = float("inf")
        self.assertEqual(self.tray.panel("codex").figure_of(0), ("∞", ""))
        codex.credits_balance = 0                                  # nothing to spend: waiting shows 0 %
        self.assertIsNone(self.tray.panel("codex").figure_of(0))

    def test_quota_columns_are_titled_by_the_time_left_until_reset(self):
        distance = tray_app.reset_distance
        self.assertEqual(distance(1000 + 4 * 3600 + 32 * 60, 1000), "4小时32分钟")
        self.assertEqual(distance(1000 + 3 * 86400 + 7 * 3600 + 50, 1000), "3天7小时")
        self.assertEqual(distance(1000 + 323 * 86400 + 5 * 3600, 1000), "323天")
        self.assertEqual(distance(1000 + 2 * 3600, 1000), "2小时")
        self.assertEqual(distance(1000 + 10, 1000), "1分钟")
        self.assertIsNone(distance(None, 1000))
        self.assertIsNone(distance(900, 1000))
        now = time.time()
        snapshot = tray_app.QuotaSnapshot("claude", "Claude Code", 300, 20.0, 80.0, int(now + 4 * 3600 + 32 * 60 + 30), now,
                                          False, None, 10080, 10.0, 90.0, int(now + 3 * 86400 + 7 * 3600 + 30))
        self.tray.snapshots["claude"] = snapshot
        self.assertEqual([item[0] for item in self.tray.panel("claude").quota], ["4小时33分钟", "3天7小时"])
        snapshot.resets_at = None      # window not started: keep its name
        self.assertEqual(self.tray.panel("claude").quota[0][0], "5 小时")

    def test_quota_line_keeps_the_known_window_and_names_an_idle_one(self):
        snapshot = tray_app.QuotaSnapshot("claude", "Claude Code", 300, None, None, None, time.time(), False, None,
                                          10080, 10.0, 90.0, int(time.time()) + 86400)
        self.assertNotIn("未知/获取失败", tray_app.quota_line(snapshot))
        self.assertIn("90%", tray_app.quota_line(snapshot))
        snapshot.used_percent, snapshot.remaining_percent = 0.0, 100.0
        self.tray.snapshots["claude"] = snapshot
        self.assertIn("剩余 100%", tray_app.quota_line(snapshot))
        self.assertIn("未开始", self.tray.menu_reset("claude"))
        self.assertIn("未开始", self.tray.menu_countdown("claude"))

    def test_account_pool_change_rebuilds_header_and_countdowns(self):
        now = time.time()
        snapshot = self.tray.snapshots["gemini"]
        native = {"key": "gemini", "label": "Gemini", "remaining_percent": 90, "resets_at": now + 3600}
        third = {"key": "claude_gpt", "label": "Claude/GPT", "remaining_percent": 50, "resets_at": now + 7200}
        snapshot.pools = [{"key": "argon", "label": "Argon", "remaining_percent": 30, "resets_at": now + 86400}, third, native]
        scene = agent_panel.Scene()
        scene.set_model(self.tray.panel("gemini"), 0)
        snapshot.pools = [third, native]
        model = self.tray.panel("gemini")
        self.assertNotIn("Argon", [q[0] for q in model.quota] + [q[2] for q in model.quota])
        scene.set_model(model, 1)
        for scale in (1, 1.25, 1.5):
            self.assertIsNotNone(scene.render(scale))

    def test_refresh_clears_only_changed_gemini_pool_history(self):
        from forecast import QuotaHistory
        from pathlib import Path
        tray = self.tray
        old = tray.snapshots["gemini"]
        old.pools = [{"key": "argon", "window_minutes": 10080}]
        now = time.time()
        fresh = tray_app.placeholder("gemini")
        fresh.pools = [{"key": "gemini", "window_minutes": 300}]
        fresh.error, fresh.used_percent, fresh.remaining_percent, fresh.updated_at = None, 20, 80, now
        tray.history = QuotaHistory(Path(self.folder.name) / "history.json")
        tray.history.add("gemini", now - 3600, 10, None)
        tray.history.add("codex", now - 3600, 10, None)
        tray.update_ui, tray.announce_resets = Mock(), Mock()
        with patch("tray_app.collect", return_value={"gemini": fresh}):
            tray.refresh_providers(["gemini"])
        self.assertEqual([sample.at for sample in tray.history.samples("gemini")], [now])
        self.assertEqual(len(tray.history.samples("codex")), 1)

    def test_forecast_replaces_schedules_in_hover_footer(self):
        self.tray.headline_forecast = Mock(return_value=("按当前速度，可用到下次重置", "ok"))
        model = self.tray.panel("codex")
        self.assertEqual((model.forecast, model.forecast_tone), ("按当前速度，可用到下次重置", "ok"))
        scene = agent_panel.Scene()
        scene.set_model(model, 0)
        self.assertIsNotNone(scene.render(1.25))

    def test_settings_event_hides_panel_before_opening_window(self):
        native = Mock(provider="codex", closing_at=None)
        native.scene.dragging = native.scene.press = None
        native.contains.return_value = True
        native.frame.return_value = [("settings",)]
        agents = Mock()
        hover = HoverDetails({}, Mock(), threading.Event(), agents=agents)
        hover.drive_panel(native, wintypes.POINT(5, 5), None, None, False, False, 10)
        native.hide.assert_called_once()
        agents.open_settings.assert_called_once()

    def test_claude_without_subscription_keeps_the_tooltip(self):
        free = tray_app.placeholder("claude")
        free.error, free.remaining_percent = "未找到订阅额度（当前 Claude 账号无订阅）", None
        self.assertFalse(tray_app.has_panel("claude", free))
        self.assertTrue(tray_app.has_panel("grok", free))
        self.assertTrue(tray_app.has_panel("claude", tray_app.placeholder("claude")))
        self.tray.snapshots["claude"] = free
        self.assertIsNone(self.tray.panel("claude"))

    def test_merge_and_split_through_clusters(self):
        model = self.tray.panel("codex")
        solo = next(b.key for b in model.bubbles if b.key == "c3")
        merged = self.tray.merge(solo, "g1")
        self.assertTrue(merged.startswith("group:"))
        self.assertIsNone(self.tray.merge("missing", "g1"))
        model = self.tray.panel("codex")
        self.assertIn(merged, [b.key for b in model.bubbles])
        self.assertEqual(sorted(self.tray.split(merged)), ["c3", "g1"])
        self.assertIsNone(self.tray.split("c3"))

    def test_open_panel_hands_drops_to_the_tray_and_closes_after_grace(self):
        panel = Mock()
        panel.provider = "codex"
        panel.closing_at = None
        panel.scene.dragging = None
        panel.contains.return_value = True
        panel.frame.return_value = [("merge", "a", "b")]
        agents = Mock()
        agents.merge.return_value = "group:ab"
        hover = HoverDetails({}, Mock(), threading.Event(), agents=agents)
        point = wintypes.POINT(5, 5)
        hover.drive_panel(panel, point, None, None, False, False, 10.0)
        agents.merge.assert_called_once_with("a", "b")
        panel.scene.absorb.assert_called_once_with("a", "b", "group:ab", 10.0)
        panel.frame.return_value = []
        panel.contains.return_value = False
        hover.drive_panel(panel, point, None, None, False, False, 10.2)
        panel.close.assert_not_called()
        hover.drive_panel(panel, point, None, None, False, False, 10.0 + hover.PANEL_GRACE + 0.1)
        panel.close.assert_called_once()


class HoverRecoveryTests(unittest.TestCase):
    def run_hover(self, factory, seconds=3.0):
        clock = [0.0]

        class Stop:
            def wait(self, _delay):
                clock[0] += 0.1
                return clock[0] > seconds

            def is_set(self):
                return clock[0] > seconds

        def sleep(_delay):
            clock[0] += 0.1

        icon = SimpleNamespace(_version4=True, hover_open=True, suppress_hover=False)
        agents = SimpleNamespace(panel=Mock(return_value=object()), settings_requested=threading.Event())
        hover = HoverDetails({'codex': icon}, lambda _: 'temporary tooltip', Stop(), agents)
        tooltip = Mock()
        with patch('hover_details.enable_thread_dpi_awareness'), \
                patch('hover_details.AgentPanelWindow', side_effect=factory) as create, \
                patch('hover_details.TrackingTooltip', return_value=tooltip) as tips, \
                patch('hover_details.notification_area_at', return_value=True), \
                patch.object(hover, 'target_at', return_value=('codex', (0, 0, 24, 24))), \
                patch('hover_details.user.GetCursorPos', return_value=True), \
                patch('hover_details.user.GetAsyncKeyState', return_value=0), \
                patch('hover_details.time.monotonic', side_effect=lambda: clock[0]), \
                patch('hover_details.time.sleep', side_effect=sleep), \
                self.assertLogs('hover_details', level='ERROR'):
            hover.run()
        return create, tips, tooltip

    @staticmethod
    def panel(error=None):
        panel = Mock(active=False, provider=None, closing_at=None)
        panel.scene = SimpleNamespace(dragging=False, press=False)
        panel.contains.return_value = True
        panel.frame = Mock(side_effect=error, return_value=[])

        def opened(provider, _model, _anchor, _now):
            panel.provider, panel.active = provider, True
        panel.open.side_effect = opened
        return panel

    def test_render_failure_recovers_without_restarting_tray(self):
        failed = self.panel(OSError(8, 'not enough memory'))
        recovered = self.panel()
        create, tips, tooltip = self.run_hover(iter([failed, recovered]).__next__)
        self.assertEqual(create.call_count, 2)
        failed.destroy.assert_called_once()
        recovered.frame.assert_called()
        tooltip.show.assert_called()
        tooltip.hide.assert_called()
        self.assertEqual(tips.call_count, 1)

    def test_initial_window_creation_failure_is_not_permanent(self):
        recovered = self.panel()
        create, _, tooltip = self.run_hover(Mock(side_effect=[OSError(8, 'not enough memory'), recovered]))
        self.assertEqual(create.call_count, 2)
        recovered.frame.assert_called()
        tooltip.show.assert_called()

    def test_repeated_failures_are_rate_limited_and_reuse_tooltip(self):
        create, tips, _ = self.run_hover(lambda: self.panel(OSError(8, 'not enough memory')), seconds=4.6)
        self.assertGreaterEqual(create.call_count, 2)
        self.assertLessEqual(create.call_count, 3)
        self.assertEqual(tips.call_count, 1)



class PanelPacingTests(unittest.TestCase):
    """The window draws only when the scene changes and reuses one bitmap."""

    def window(self, scene):
        import hover_details
        window = hover_details.AgentPanelWindow.__new__(hover_details.AgentPanelWindow)
        window.hwnd, window.surface, window.scene = 1, Mock(), scene
        window.provider, window.anchor, window.work, window.scale = "codex", (900, 1040, 924, 1064), (0, 0, 1920, 1040), 1.0
        window.rect = window.closing_at = window.image = window.placed = None
        window.opened_at, window.shown, window.checked_at, window.drawn_at = 0.0, True, 0.0, 0.0
        return window

    @staticmethod
    def scene(**state):
        scene = Mock(dragging=None, press=None, bodies={}, **state)
        scene.step.return_value = []
        scene.render.return_value = Image.new("RGBA", (448, 400))
        return scene

    def test_resting_panel_is_not_redrawn(self):
        scene = self.scene(dirty=False, animating=False, ambient=False)
        window = self.window(scene)
        with patch("hover_details.is_window_covered", return_value=False):
            for index in range(60):
                window.frame(None, False, False, 1.0 + index / 60)
        self.assertEqual(scene.render.call_count, 1)                 # the first frame only
        self.assertEqual(window.surface.present.call_count, 1)
        window.surface.move.assert_not_called()                      # same place, same opacity

    def test_moving_scene_draws_every_tick_and_ambient_at_thirty_fps(self):
        scene = self.scene(dirty=False, animating=True, ambient=True)
        window = self.window(scene)
        with patch("hover_details.is_window_covered", return_value=False):
            for index in range(30):
                window.frame(None, False, False, 1.0 + index / 60)
            self.assertEqual(scene.render.call_count, 30)
            scene.animating = False
            scene.render.reset_mock()
            for index in range(60):
                window.frame(None, False, False, 2.0 + index / 60)
        self.assertIn(scene.render.call_count, range(28, 33))

    def test_opening_fade_moves_without_rerendering(self):
        scene = self.scene(dirty=False, animating=False, ambient=False)
        window = self.window(scene)
        window.opened_at = 1.0
        with patch("hover_details.is_window_covered", return_value=False):
            for index in range(15):
                window.frame(None, False, False, 1.0 + index / 60)
        self.assertEqual(scene.render.call_count, 1)
        alphas = [c.args[3] for c in window.surface.move.call_args_list]
        self.assertTrue(alphas and alphas == sorted(alphas) and alphas[-1] == 255)

    def test_surface_reuses_one_bitmap_between_frames(self):
        import hover_details
        created = []

        def dib(_dc, _header, _usage, bits, *_):
            bits._obj.value = 1
            created.append(len(created) + 100)
            return created[-1]
        with patch("hover_details.gdi.CreateCompatibleDC", return_value=5), \
                patch("hover_details.gdi.CreateDIBSection", side_effect=dib), \
                patch("hover_details.gdi.SelectObject", return_value=7) as select, \
                patch("hover_details.gdi.DeleteObject") as delete, \
                patch("hover_details.gdi.DeleteDC") as delete_dc, \
                patch("hover_details.ctypes.memmove"), \
                patch("hover_details.user.UpdateLayeredWindow", return_value=True) as update:
            surface = hover_details.LayeredSurface()
            for height in (400, 380, 400, 400):
                surface.present(1, Image.new("RGBA", (448, height)), 10, 20)
            surface.move(1, 10, 18, 128)
            self.assertEqual(created, [100])
            surface.present(1, Image.new("RGBA", (448, 520)), 10, 20)    # taller: one new bitmap
            self.assertEqual(created, [100, 101])
            delete.assert_called_once_with(100)
            surface.close()
        self.assertEqual(update.call_count, 6)
        self.assertEqual(select.call_args_list[-1], call(5, 7))          # original bitmap restored
        delete_dc.assert_called_once_with(5)


class ModelIqTrayTests(unittest.TestCase):
    def test_banner_intel_follows_key_scores_and_saved_choice(self):
        import tempfile
        from pathlib import Path
        import model_iq
        from test_model_iq import PAYLOAD
        tray = QuotaTray.__new__(QuotaTray)
        tray.iq_state, tray.iq_config = model_iq.IqState(), {"key": "", "shown": None}
        tray.iq_wake, tray.iq_lock, tray.update_ui = threading.Event(), threading.Lock(), Mock()
        now = time.time()
        with patch.dict("os.environ", {model_iq.ENV_KEY: ""}):
            self.assertEqual(tray.panel_intel(now), ((), "未设置"))
            tray.iq_config["key"] = "asl_live_abcdefgh"
            self.assertEqual(tray.panel_intel(now), ((), "读取中"))
            with patch.object(model_iq, "_request", return_value=PAYLOAD), tempfile.TemporaryDirectory() as folder, \
                    patch.object(tray_app, "IQ_STATE_PATH", Path(folder) / "s.json"), \
                    patch.object(tray_app, "IQ_CONFIG_PATH", Path(folder) / "c.json"):
                self.assertIn("已读取 5", tray.refresh_model_iq())
                intel, note = tray.panel_intel(time.time())
                self.assertEqual([item[0] for item in intel], ["GPT-5.6 Sol", "Opus 4.7", "Gemini 3 Pro"])
                tray.save_model_iq("asl_live_abcdefgh", ["gemini-3-pro-preview"])
                self.assertEqual(tray.panel_intel(time.time())[0], (("Gemini 3 Pro", "70", "ok", False),))
                intel, _ = tray.panel_intel(time.time(), ["claude-sonnet-4-5"])     # the TPS model leads
                self.assertEqual(intel[0], ("Sonnet 4.5", "66", "ok", True))
                tray.iq_state.blocked_day, tray.iq_state.error = model_iq.utc_day(time.time()), "今日免费额度已用完"
                self.assertEqual(tray.panel_intel(time.time() + 40 * 3600)[1], "")     # last result, no quota note
                self.assertTrue(tray.panel_intel(time.time() + 40 * 3600)[0])
                self.assertFalse(tray.iq_wake.is_set())          # same key: no extra call
                self.assertEqual(tray.model_iq_status()["calls"], 1)
