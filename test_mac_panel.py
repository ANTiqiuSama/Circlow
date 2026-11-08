"""macOS hover panel: geometry, headless tray wiring and menu bar stand-ins.
The AppKit window itself is exercised on a Mac by hand; these stay pure."""
import time
import unittest
from unittest.mock import Mock, patch

import mac_panel
from detail_card import MARGIN


class GeometryTest(unittest.TestCase):
    # A 30 x 24 pt status item at the top of a 1512 x 982 screen whose
    # visible frame ends under a 33 pt menu bar.
    ANCHOR = ((1063.0, 949.0), (30.0, 33.0))
    VISIBLE = ((0.0, 0.0), (1512.0, 949.0))

    def test_panel_hangs_under_its_item_with_the_card_gap(self):
        x, y = mac_panel.panel_origin(self.ANCHOR, (448.0, 467.0), self.VISIBLE)
        self.assertEqual(x, 1063 + 15 - 224)
        # Card top = panel top - MARGIN sits GAP points under the item.
        self.assertEqual(y + 467 - MARGIN, 949 - mac_panel.GAP)

    def test_panel_stays_on_screen_near_the_edges(self):
        right = ((1490.0, 949.0), (22.0, 33.0))
        x, _ = mac_panel.panel_origin(right, (448.0, 467.0), self.VISIBLE)
        self.assertEqual(x, 1512 - 448)
        left = ((4.0, 949.0), (22.0, 33.0))
        x, _ = mac_panel.panel_origin(left, (448.0, 467.0), self.VISIBLE)
        self.assertEqual(x, 0)
        tall = mac_panel.panel_origin(self.ANCHOR, (448.0, 2000.0), self.VISIBLE)
        self.assertEqual(tall[1], 0)

    def test_local_point_flips_y_and_removes_the_margin(self):
        frame = ((858.0, 490.0), (448.0, 467.0))
        self.assertEqual(mac_panel.local_point(frame, (858 + MARGIN, 490 + 467 - MARGIN)), (0, 0))
        self.assertEqual(mac_panel.local_point(frame, (858 + MARGIN + 10, 490 + 467 - MARGIN - 20)), (10, 20))

    def test_contains_uses_slack(self):
        frame = ((10.0, 10.0), (100.0, 50.0))
        self.assertTrue(mac_panel.contains(frame, (10, 10)))
        self.assertFalse(mac_panel.contains(frame, (110, 30)))
        self.assertTrue(mac_panel.contains(frame, (114, 30), slack=6))


class HoverPanelLogicTest(unittest.TestCase):
    def controller(self):
        icons = {"codex": Mock(details_visible=False), "grok": Mock(details_visible=False)}
        hover = mac_panel.MacHoverPanel(Mock(), {}, icons, Mock())
        hover._schedule = Mock()
        return hover, icons

    def test_open_menu_hides_the_panel_and_suppresses_until_the_pointer_leaves(self):
        hover, _ = self.controller()
        hover.window = Mock(active=True)
        hover.menu_will_open("codex")
        hover.window.hide.assert_called_once()
        hover.menu_did_close("codex")
        self.assertIn("codex", hover.suppressed)
        item = Mock()
        item.button.return_value.window.return_value.frame.return_value = Mock(
            origin=Mock(x=0, y=0), size=Mock(width=20, height=20))
        hover.items = {"codex": item}
        hover.release_suppressed((10, 10))
        self.assertIn("codex", hover.suppressed)      # still on the item
        hover.release_suppressed((300, 300))
        self.assertNotIn("codex", hover.suppressed)

    def test_tick_marks_only_the_shown_source_and_slows_down_when_idle(self):
        hover, icons = self.controller()
        hover.pointer = Mock(return_value=((500.0, 500.0), False, False))
        hover.target_at = Mock(return_value=(None, None, None))
        hover.tick()
        self.assertFalse(icons["codex"].details_visible)
        hover._schedule.assert_called_with(hover.SLOW)

    def test_a_failed_frame_backs_off_instead_of_raising(self):
        hover, _ = self.controller()
        hover.pointer = Mock(return_value=((1.0, 1.0), False, False))
        hover.target_at = Mock(side_effect=RuntimeError("boom"))
        with self.assertLogs("Circlow", "ERROR"):
            hover.tick()
        self.assertIsNone(hover.window)
        self.assertGreater(hover.retry_at, time.monotonic())


class HeadlessTrayTest(unittest.TestCase):
    def test_quota_tray_accepts_stand_in_icons_without_windows_parts(self):
        import mac_menu
        import tray_app
        icons = {key: mac_menu.MacIcon(key) for key in tray_app.PROVIDERS}
        with patch.object(tray_app.QuotaTray, "migrate_startup") as migrate:
            tray = tray_app.QuotaTray(icons=icons)
        migrate.assert_not_called()
        self.assertIs(tray.icons, icons)
        self.assertEqual(tray.slots, list(icons.values()))
        self.assertFalse(tray.maybe_reorder(time.time(), force=True))  # nothing to reorder on macOS
        key = next(iter(icons))
        tray.update_icon(key, tray.letters[key])
        self.assertEqual(icons[key].icon.size, (64, 64))
        self.assertTrue(icons[key].title)


class MenuBarStandInTest(unittest.TestCase):
    def test_notify_quotes_applescript_strings(self):
        import mac_menu
        popen = Mock()
        mac_menu.notify('Codex "重置"', 'a\\b "c"', popen=popen)
        script = popen.call_args.args[0][2]
        self.assertEqual(script, 'display notification "a\\\\b \\"c\\"" with title "Codex \\"重置\\""')

    def test_mac_icon_has_the_attributes_the_tray_mixins_touch(self):
        import mac_menu
        icon = mac_menu.MacIcon("codex")
        for name in ("icon", "title", "visible", "details_visible", "hover_open"):
            self.assertTrue(hasattr(icon, name))
        icon.update_menu()
        icon.stop()
        with patch.object(mac_menu, "notify") as notify:
            icon.notify("msg", "title")
        notify.assert_called_once_with("title", "msg")


class ClickRoutingTest(unittest.TestCase):
    """macOS clicks: desktop clients for Codex / Gemini / Claude, Grok in Herdr."""

    def ways(self, herdr="/opt/homebrew/bin/herdr", apps=True):
        import herdr_source
        import launchers
        return (patch.object(herdr_source, "find_exe", return_value=herdr),
                patch.object(launchers, "mac_app_installed", return_value=apps))

    def test_clients_open_by_bundle_id(self):
        import mac_menu
        run = Mock(return_value=Mock(returncode=0))
        herdr, apps = self.ways()
        with herdr, apps, patch("launchers.Path.is_dir", return_value=False):
            self.assertEqual(mac_menu.open_source("codex", Mock(), run=run), "mac-app")
            self.assertEqual(run.call_args.args[0], ["open", "-b", "com.openai.codex"])
            mac_menu.open_source("gemini", Mock(), run=run)
            self.assertEqual(run.call_args.args[0], ["open", "-b", "com.google.antigravity"])
            state = Mock()
            self.assertEqual(mac_menu.open_source("claude", state, run=run), "mac-app")
            self.assertEqual(run.call_args.args[0], ["open", "-b", "com.anthropic.claudefordesktop"])
            state.open_in_herdr.assert_not_called()

    def test_grok_goes_to_herdr_and_a_failed_claude_app_falls_back(self):
        import mac_menu
        state = Mock()
        herdr, apps = self.ways()
        with herdr, apps:
            self.assertEqual(mac_menu.open_source("grok", state, run=Mock()), "herdr")
            state.open_in_herdr.assert_called_once_with("grok")
            state.reset_mock()
            failed = Mock(return_value=Mock(returncode=1))
            with self.assertLogs("Circlow", "WARNING"):
                self.assertEqual(mac_menu.open_source("claude", state, run=failed), "herdr")
            state.open_in_herdr.assert_called_once_with("claude")

    def test_herdr_and_cli_windows_open_in_ghostty_else_terminal(self):
        import herdr_source
        args = herdr_source.mac_terminal(["/opt/homebrew/bin/herdr"], "/Users/me", ghostty="/Applications/Ghostty.app")
        self.assertEqual(args, ["open", "-na", "/Applications/Ghostty.app", "--args",
                                "--working-directory=/Users/me", "-e", "/opt/homebrew/bin/herdr"])
        fallback = herdr_source.mac_terminal(["/opt/homebrew/bin/herdr"], "/Users/me", ghostty=None)
        self.assertEqual(fallback[0], "osascript")
        popen = Mock()
        with patch.object(herdr_source.sys, "platform", "darwin"), \
                patch.object(herdr_source, "ghostty_app", return_value="/Applications/Ghostty.app"):
            herdr_source.open_window("/opt/homebrew/bin/herdr", popen)
        self.assertEqual(popen.call_args.args[0][:3], ["open", "-na", "/Applications/Ghostty.app"])

    def test_a_click_drops_the_panel_until_the_pointer_leaves(self):
        hover = mac_panel.MacHoverPanel(Mock(), {}, {}, Mock())
        hover.window = Mock(active=True)
        hover.clicked("grok")
        hover.window.hide.assert_called_once()
        self.assertIn("grok", hover.suppressed)


class ClaudeProbeTest(unittest.TestCase):
    def test_the_trust_dialog_is_answered_down_then_enter(self):
        import cli_usage
        typed = []
        with patch.object(cli_usage.time, "sleep") as pause:
            reply = dict((p.pattern, r) for p, r in cli_usage.CLAUDE_PROBE.answers)[cli_usage.CLAUDE_TRUST_NO_RE.pattern]
            cli_usage._type_reply(typed.append, reply)
        self.assertEqual(typed, ["\x1b[B", "\r"])
        # It waits for the dialog to take keys, then lets the marker move before Enter.
        self.assertEqual([call.args[0] for call in pause.call_args_list], [1.5, 0.6])
        with patch.object(cli_usage.time, "sleep") as pause:
            cli_usage._type_reply(typed.append, ("a", "b"))     # back-to-back keys get KEY_PAUSE
        pause.assert_called_once_with(cli_usage.KEY_PAUSE)
        cli_usage._type_reply(typed.append, "x")
        self.assertEqual(typed[-1], "x")

    def test_probe_session_never_sees_the_auto_mode_question(self):
        import json
        import os
        import cli_usage
        if os.name == "nt":
            self.skipTest("POSIX argument list")
        index = cli_usage.CLAUDE_ARGS.index("--settings")
        self.assertEqual(json.loads(cli_usage.CLAUDE_ARGS[index + 1]), {"permissions": {"defaultMode": "default"}})



class LiquidGlassTest(unittest.TestCase):
    def tearDown(self):
        import agent_panel
        agent_panel.set_glass(False)

    def test_glass_mode_renders_the_content_on_a_transparent_card_of_the_same_size(self):
        import agent_panel
        from PIL import Image
        content = Image.new("RGBA", (100, 60), (240, 238, 230, 255))
        agent_panel.set_glass(False)
        drawn = agent_panel._card(content, 2.0)
        agent_panel.set_glass(True)
        glass = agent_panel._card(content, 2.0)
        self.assertEqual(glass.size, drawn.size)                       # same geometry and margins
        self.assertEqual(glass.getpixel((2, 2))[3], 0)                 # no drawn shadow or surface
        margin = round(MARGIN * 2.0)
        self.assertEqual(glass.getpixel((margin + 5, margin + 5)), (240, 238, 230, 255))

    def test_switching_modes_clears_the_cached_card(self):
        import agent_panel
        agent_panel._background.cache_clear()
        agent_panel.set_glass(True)
        info = agent_panel._background.cache_info()
        self.assertEqual(info.currsize, 0)
        self.assertTrue(agent_panel.GLASS)

    def test_glass_can_be_turned_off_and_is_rounded_and_tinted(self):
        import os
        with patch.dict(os.environ, {"CIRCLOW_GLASS": "0"}):
            self.assertFalse(mac_panel.glass_available())
            self.assertIsNone(mac_panel.make_glass())
        if not mac_panel.glass_available():
            self.skipTest("no NSGlassEffectView on this system")
        glass = mac_panel.make_glass()
        self.assertEqual(float(glass.cornerRadius()), mac_panel.GLASS_RADIUS)
        self.assertAlmostEqual(float(glass.tintColor().alphaComponent()), mac_panel.GLASS_TINT[3], places=3)

if __name__ == "__main__":
    unittest.main()
