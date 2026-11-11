"""macOS agent panel: geometry, click logic, headless tray wiring and menu bar stand-ins.
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


    def test_only_the_visible_card_counts_as_the_panel(self):
        size = (448.0, 467.0)
        frame = (mac_panel.panel_origin(self.ANCHOR, size, self.VISIBLE), size)
        (x, y), (w, h) = frame
        card_top = y + h - MARGIN
        self.assertTrue(mac_panel.on_card((x + w / 2, y + h / 2), frame))              # the card
        self.assertTrue(mac_panel.on_card((x + MARGIN + 1, card_top - 1), frame))      # its top-left corner
        self.assertFalse(mac_panel.on_card((x + MARGIN / 2, y + h / 2), frame))        # shadow margin only
        self.assertFalse(mac_panel.on_card((1078, card_top + 4), frame))               # the gap under the item
        self.assertFalse(mac_panel.on_card((x + w / 2, y + MARGIN - 6), frame))        # under the card
        self.assertFalse(mac_panel.on_card(None, frame))


class PanelLogicTest(unittest.TestCase):
    """The panel opens on a click and closes on the next press off its card; nothing opens on hover."""
    ANCHOR = ((1063.0, 949.0), (30.0, 33.0))
    FRAME = ((854.0, 476.0), (448.0, 467.0))
    AWAY = (300.0, 300.0)
    ON_ITEM = (1078.0, 960.0)
    ON_CARD = (854.0 + 224, 476.0 + 233)

    def controller(self, model="model"):
        icons = {"codex": Mock(details_visible=False), "grok": Mock(details_visible=False)}
        tray = Mock()
        tray.panel.return_value = model
        panel = mac_panel.MacPanel(tray, {}, icons, Mock())
        panel._run = Mock()
        panel.pointer = Mock(return_value=(self.AWAY, False, False))
        panel.anchor_of = Mock(return_value=(self.ANCHOR, Mock()))
        return panel, icons

    def window(self):
        """A stand-in PanelWindow that tracks open and hide like the real one."""
        window = Mock(provider=None, anchor=None, frame=self.FRAME, active=False)

        def opened(provider, _model, anchor, _screen, _now):
            window.provider, window.anchor, window.active = provider, anchor, True

        def hidden():
            window.provider, window.active = None, False
        window.open.side_effect = opened
        window.hide.side_effect = hidden
        window.on_card.side_effect = lambda point: mac_panel.on_card(point, self.FRAME)
        window.step.return_value = []
        window.scene = Mock(press=None, dragging=False)
        return window

    def shown(self):
        panel, icons = self.controller()
        window = self.window()
        with patch.object(mac_panel, "PanelWindow", return_value=window):
            self.assertTrue(panel.toggle("codex"))
        return panel, icons, window

    def point(self, panel, at, left=False, right=False):
        panel.pointer = Mock(return_value=(at, left, right))
        panel.tick()

    def test_a_click_opens_the_panel_and_a_second_click_closes_it(self):
        panel, icons, window = self.shown()
        self.assertEqual(panel.shown, "codex")
        window.open.assert_called_once()
        self.assertTrue(icons["codex"].details_visible)        # the shown source reads at the fastest tier
        self.assertFalse(icons["grok"].details_visible)
        panel._run.assert_called_with(True)
        self.assertTrue(panel.toggle("codex"))                 # the same item again
        self.assertIsNone(panel.shown)
        self.assertFalse(icons["codex"].details_visible)
        panel._run.assert_called_with(False)                   # no timer while nothing is shown

    def test_a_click_on_another_item_shows_that_source(self):
        panel, icons, window = self.shown()
        with patch.object(mac_panel, "PanelWindow", return_value=window):
            self.assertTrue(panel.toggle("grok"))
        self.assertEqual(panel.shown, "grok")
        self.assertTrue(icons["grok"].details_visible)
        self.assertFalse(icons["codex"].details_visible)

    def test_a_source_without_a_panel_has_nothing_to_show(self):
        panel, _ = self.controller(model=None)
        with patch.object(mac_panel, "PanelWindow") as build:
            self.assertFalse(panel.toggle("codex"))            # the caller shows its menu instead
        build.assert_not_called()
        self.assertIsNone(panel.shown)

    def test_nothing_opens_on_hover(self):
        panel, icons = self.controller()
        with patch.object(mac_panel, "PanelWindow") as build:
            for _ in range(120):                               # two seconds resting on the item
                self.point(panel, self.ON_ITEM)
        build.assert_not_called()
        self.assertIsNone(panel.shown)
        self.assertFalse(icons["codex"].details_visible)

    def test_the_panel_stays_until_a_press_off_the_card(self):
        panel, _, _ = self.shown()
        self.point(panel, self.AWAY)                           # the pointer leaving is not a reason to close
        self.assertEqual(panel.shown, "codex")
        panel.mouse_down(self.AWAY)                            # a click on something else
        self.assertIsNone(panel.shown)
        panel.mouse_down(self.AWAY)                            # nothing shown: nothing to do

    def test_a_press_in_the_shadow_margin_closes_it_too(self):
        panel, _, _ = self.shown()
        (x, y), (_w, h) = self.FRAME
        panel.mouse_down((x + MARGIN / 2, y + h / 2))
        self.assertIsNone(panel.shown)

    def test_presses_on_the_card_and_on_its_item_keep_it(self):
        panel, _, _ = self.shown()
        panel.mouse_down(self.ON_CARD)
        self.assertEqual(panel.shown, "codex")
        self.point(panel, self.AWAY, left=True)                # dragged off the card with the button held
        self.assertEqual(panel.shown, "codex")
        panel.mouse_down(self.ON_ITEM)                         # on its own item: the item's click toggles it
        self.assertEqual(panel.shown, "codex")

    def test_the_gear_closes_the_panel_and_opens_settings(self):
        panel, _, window = self.shown()
        window.step.return_value = [("settings", "sources")]
        self.point(panel, self.ON_CARD)
        self.assertIsNone(panel.shown)
        panel.on_settings.assert_called_once_with("codex", "sources")

    def test_a_failed_frame_backs_off_instead_of_raising(self):
        panel, icons, window = self.shown()
        window.step.side_effect = RuntimeError("boom")
        with self.assertLogs("Circlow", "ERROR"):
            self.point(panel, self.ON_CARD)
        self.assertIsNone(panel.window)
        self.assertGreater(panel.retry_at, time.monotonic())
        self.assertFalse(icons["codex"].details_visible)
        self.assertFalse(panel.toggle("codex"))                # backing off: the caller shows the menu


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
        content = Image.new("RGBA", (400, 300), (240, 238, 230, 255))
        agent_panel.set_glass(False)
        drawn = agent_panel._card(content, 2.0)
        agent_panel.set_glass(True)
        glass = agent_panel._card(content, 2.0)
        self.assertEqual(glass.size, drawn.size)                       # same geometry and margins
        self.assertEqual(glass.getpixel((2, 2))[3], 0)                 # no drawn shadow or surface
        margin = round(MARGIN * 2.0)
        self.assertEqual(glass.getpixel((margin + 200, margin + 150)), (240, 238, 230, 255))   # content as drawn
        # No sheen under or over the content: the glass shows as it is, and the rim is added by background().
        self.assertEqual(glass.getpixel((margin + 200, margin + 1)), (240, 238, 230, 255))
        clear = agent_panel._card(Image.new("RGBA", (400, 300), (0, 0, 0, 0)), 2.0)
        self.assertEqual(clear.getpixel((margin + 200, margin + 6))[3], 0)
        self.assertEqual(clear.getpixel((margin + 200, margin + 294))[3], 0)
        # The rim, LumaCenter's GlassRim: a one-point hairline, white at 22 % along the top edge,
        # 12 % along the bottom, 3 % at the middle of the sides.
        rim = agent_panel._rim((400, 300), 2.0)
        top, bottom, side = rim.getpixel((200, 0))[3], rim.getpixel((200, 299))[3], rim.getpixel((0, 150))[3]
        self.assertAlmostEqual(top, round(0.22 * 255), delta=1)
        self.assertAlmostEqual(bottom, round(0.12 * 255), delta=1)
        self.assertAlmostEqual(side, round(0.03 * 255), delta=1)
        self.assertAlmostEqual(rim.getpixel((200, 1))[3], top, delta=1)   # one point: two pixels at 2x
        self.assertEqual(rim.getpixel((200, 2))[3], 0)
        self.assertEqual(rim.getpixel((200, 150))[3], 0)             # nothing inside the hairline

    def test_the_rendered_card_carries_the_rim_at_its_final_height(self):
        import agent_panel
        agent_panel.set_glass(True)
        model = agent_panel.PanelModel("codex", "C", "GPT / Codex", "2 个在工作", (("5 小时", 70.0), ("每周", 40.0)), 80.0, ())
        scene = agent_panel.Scene()
        scene.set_model(model, 0)
        frame = scene.render(2.0)
        margin, x = round(MARGIN * 2.0), frame.width // 2
        self.assertAlmostEqual(frame.getpixel((x, margin))[3], round(0.22 * 255), delta=1)                        # top edge
        self.assertAlmostEqual(frame.getpixel((x, frame.height - margin - 1))[3], round(0.12 * 255), delta=1)     # bottom edge
        self.assertAlmostEqual(frame.getpixel((margin, frame.height // 2))[3], round(0.03 * 255), delta=2)        # mid side
        self.assertEqual(frame.getpixel((x, margin + 4))[3], 0)                                                   # no sheen inside

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
        self.assertEqual(int(glass.style()), 1)                        # the clear style: dark and see-through

if __name__ == "__main__":
    unittest.main()
